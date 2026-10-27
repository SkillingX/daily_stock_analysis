# -*- coding: utf-8 -*-
"""
Multi-provider LLM Tool-Calling Adapter.

Normalizes function-calling / tool-use across all providers into a unified
interface consumed by the AgentExecutor, via LiteLLM.
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Dict, Iterable, List, Optional, Tuple, cast

import litellm
from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper
from litellm.router import Router

from src.llm.attempt import (
    STATUS_OK,
    STATUS_PARSE_ERROR,
    attempt_entry,
    current_attempt,
    last_attempt,
)
from src.llm.concurrency import limited_acompletion
from src.config import (
    extra_litellm_params,
    get_api_keys_for_model,
    get_config,
    get_configured_llm_models,
    get_effective_agent_models_to_try,
    get_effective_agent_primary_model,
)
from src.agent.provider_trace import (
    TRACE_MODEL_KEY,
    TRACE_PROVIDER_KEY,
    resolved_model_provider_identity,
    resolved_provider_namespace,
    trace_model_matches,
)
from src.core.research_run import ResearchStopped, current_research
from src.llm.errors import classify_litellm_generation_param_error
from src.llm.generation_params import (
    apply_litellm_generation_params,
    apply_litellm_param_recovery,
    apply_minimax_reasoning_params,
    remember_litellm_generation_param_recovery,
    resolve_litellm_wire_model,
)
from src.llm.usage import (
    attach_message_hmacs,
    extract_usage_payload,
    normalize_litellm_usage,
)

logger = logging.getLogger(__name__)


class _StreamAttemptRouter(Router):
    """Router that exposes the inner stream wrapper to the current attempt.

    ``Router._acompletion_streaming_iterator`` receives the real litellm
    ``CustomStreamWrapper`` and unconditionally wraps it in a fallback
    iterator whose ``completion_stream`` is an opaque async generator. This
    override records the wrapper on the current attempt (a mutable object
    visible across the Router's context) before deferring to the original
    implementation, so the stream consumer can attach its provider-usage
    observer at the raw SDK boundary. No routing, fallback or SDK parsing is
    replaced; on an SDK without this method the observation degrades to
    unknown usage.
    """

    async def _acompletion_streaming_iterator(self, model_response: Any, **kwargs: Any) -> Any:
        attempt = current_attempt.get()
        if attempt is not None and attempt.stream_transport is None:
            attempt.stream_transport = model_response
        return await super()._acompletion_streaming_iterator(model_response=model_response, **kwargs)

# Sentinel for ``_parse_litellm_response(stream_raw_usage=...)``: the default
# keeps the non-stream path reading usage off the full response object, while
# a streamed call always passes an explicit value (provider-actual dict or
# None) so LiteLLM's synthesized stream estimates can never be promoted.
_NON_STREAM_USAGE = object()


def _resolve_litellm_exception(name: str) -> type[BaseException]:
    """Return a catchable LiteLLM exception class even in stubbed test environments."""
    exc = getattr(litellm, name, None)
    if isinstance(exc, type) and issubclass(exc, BaseException):
        return exc

    class _FallbackLiteLLMError(Exception):
        pass

    _FallbackLiteLLMError.__name__ = f"Fallback{name}"
    return _FallbackLiteLLMError


# ============================================================
# Unified response types
# ============================================================


@dataclass
class ToolCall:
    """A single tool call requested by the LLM."""

    id: str
    name: str
    arguments: Dict[str, Any]
    thought_signature: Optional[str] = None
    provider_specific_fields: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    """Normalized response from any LLM provider."""

    content: Optional[str] = None  # text response (final answer)
    tool_calls: List[ToolCall] = field(default_factory=list)  # tool calls to execute
    reasoning_content: Optional[str] = (
        None  # Chain-of-thought (CoT) from DeepSeek thinking mode; must be passed back in multi-turn assistant messages; None for other providers
    )
    provider_blocks: List[Dict[str, Any]] = field(
        default_factory=list
    )  # Opaque provider content blocks (e.g. Claude thinking/redacted_thinking)
    usage: Dict[str, Any] = field(default_factory=dict)  # token usage info
    provider: str = ""  # which provider handled this call
    model: str = (
        ""  # full model name used (e.g. gemini/gemini-2.0-flash), for report meta
    )
    raw: Any = None  # raw provider response for debugging
    finish_reason: Optional[str] = None


# Models that auto-return reasoning_content; do NOT send extra_body (may cause 400).
_AUTO_THINKING_MODELS: List[str] = ["deepseek-reasoner", "deepseek-r1", "qwq"]

# Models that need explicit opt-in via extra_body; payload decoupled from model name.
_OPT_IN_THINKING_MODELS: Dict[str, dict[str, Any]] = {
    "deepseek-chat": {"thinking": {"type": "enabled"}},
}

# Custom model pricing for models not in LiteLLM's built-in price list.
# Official MiniMax pricing: https://platform.minimax.io/docs/guides/pricing-paygo
# - MiniMax-M3: $0.6/M input tokens, $2.4/M output tokens for prompts <=512K input
#   tokens. Officially supports up to 1M input tokens with a separate higher
#   price tier for the >512K bucket; we conservatively register only the
#   <=512K bucket here because the cost tracker carries a single per-token
#   price and the higher-tier price is not modeled. Long prompts will be
#   cost-estimated using the <=512K rate; treat the estimate as a floor in
#   that case.
# - MiniMax-M2.7: $0.3/M input tokens, $1.2/M output tokens.
# - MiniMax-M2.5: kept as legacy so existing user configs continue to report
#   accurate cost. Still listed as a Legacy Model on the official pricing
#   page; remove only after we have user-facing migration guidance.
# - MiniMax-M3.1-Flash: $0.15/M input, $0.60/M output, $0.015/M cache reads;
#   1M context. These pay-as-you-go list prices are usage estimates, not
#   subscription charges. The published docs do not state a max output limit.
_CUSTOM_MODEL_PRICING: Dict[str, dict[str, Any]] = {
    "MiniMax-M3.1-Flash": {
        "supports_function_calling": True,
        "supports_vision": True,
        "supports_audio_input": False,
        "supports_audio_output": False,
        "context_window": 1000000,
        "max_input_tokens": 1000000,
        "input_cost_per_token": 0.00000015,
        "output_cost_per_token": 0.0000006,
        "cache_read_input_token_cost": 0.000000015,
    },
    "MiniMax-M3": {
        "supports_function_calling": True,
        "supports_vision": True,
        "supports_audio_input": False,
        "supports_audio_output": False,
        # Project-conservative bound for the <=512K input-token price tier.
        # MiniMax-M3 supports up to 1M input tokens officially, but pricing
        # changes above 512K; see comment block above.
        "context_window": 512000,
        "max_tokens": 128000,
        "input_cost_per_token": 0.0000006,  # $0.6 / 1M tokens (<=512K input bucket)
        "output_cost_per_token": 0.0000024,  # $2.4 / 1M tokens (<=512K input bucket)
    },
    "MiniMax-M2.7": {
        "supports_function_calling": True,
        "supports_vision": False,
        "supports_audio_input": False,
        "supports_audio_output": False,
        "context_window": 100000,
        "max_tokens": 10000,
        "input_cost_per_token": 0.0000003,  # $0.3 / 1M tokens
        "output_cost_per_token": 0.0000012,  # $1.2 / 1M tokens
    },
    # Legacy model retained for backward compatibility with existing user
    # configs; values match the previous M2.5 entry to avoid silently
    # zero-costing prior cost estimates.
    "MiniMax-M2.5": {
        "supports_function_calling": True,
        "supports_vision": False,
        "supports_audio_input": False,
        "supports_audio_output": False,
        "context_window": 245760,
        "max_tokens": 8192,
        "input_cost_per_token": 0.0000003,  # $0.3 / 1M tokens (legacy)
        "output_cost_per_token": 0.0000012,  # $1.2 / 1M tokens (legacy)
    },
}

_FALLBACK_MODEL_PRICING: Dict[str, Any] = {
    "supports_function_calling": True,
    "supports_vision": False,
    "supports_audio_input": False,
    "supports_audio_output": False,
    "context_window": 100000,
    "max_tokens": 10000,
    "input_cost_per_token": 0.0,
    "output_cost_per_token": 0.0,
}
_FALLBACK_MODEL_PRICING_REGISTERED: set[str] = set()


def _split_provider_model(model: str) -> Tuple[str, str]:
    normalized = (model or "").strip()
    if not normalized:
        return "", ""
    if "/" in normalized:
        provider, remainder = normalized.split("/", 1)
        return provider.lower(), remainder.strip()
    return "openai", normalized


def _object_to_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    if hasattr(value, "dict"):
        try:
            dumped = value.dict()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    result: Dict[str, Any] = {}
    for key in ("type", "text", "content", "thinking", "signature", "data"):
        if hasattr(value, key):
            result[key] = getattr(value, key)
    return result


def _provider_specific_fields_from(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    data = _object_to_dict(value)
    return data if isinstance(data, dict) else {}


def _extract_provider_blocks(choice: Any) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Return opaque provider blocks and joined text block content, if present."""
    block_sources = []
    message = getattr(choice, "message", None)
    for owner in (message, choice):
        if owner is None:
            continue
        for attr in ("content", "content_blocks", "provider_blocks", "thinking_blocks"):
            value = getattr(owner, attr, None)
            if isinstance(value, list):
                block_sources.append(value)

    blocks: List[Dict[str, Any]] = []
    text_parts: List[str] = []
    for source in block_sources:
        for raw_block in source:
            block = _object_to_dict(raw_block)
            if not block:
                continue
            blocks.append(block)
            block_type = str(block.get("type") or "")
            text = block.get("text") or block.get("content")
            if block_type == "text" and text:
                text_parts.append(str(text))
    return blocks, ("".join(text_parts).strip() or None)


def _message_trace_matches_target(
    message: Dict[str, Any],
    target_model: Optional[str],
    *,
    target_provider: Optional[str] = None,
) -> bool:
    """Whether provider-specific fields in ``message`` can be sent to target."""
    if not target_model:
        return True
    trace_provider = message.get(TRACE_PROVIDER_KEY)
    trace_model = message.get(TRACE_MODEL_KEY)
    if not trace_provider and not trace_model:
        return True
    return trace_model_matches(
        trace_provider,
        trace_model,
        target_model,
        current_provider=target_provider,
    )


def _model_matches(model: str, entries: List[str]) -> bool:
    """Check if model name matches any entry (exact or prefix with version suffix)."""
    if not model:
        return False
    m = model.lower().strip()
    for e in entries:
        if m == e or m.startswith(e + "-"):
            return True
    return False


def _get_opt_in_payload(
    model: str, opt_in: Dict[str, dict[str, Any]]
) -> Optional[dict[str, Any]]:
    """Return extra_body payload for opt-in thinking models, or None."""
    if not model:
        return None
    m = model.lower().strip()
    for key, payload in opt_in.items():
        if m == key or m.startswith(key + "-"):
            return payload
    return None


def get_thinking_extra_body(model: str) -> Optional[dict[str, Any]]:
    """Return extra_body for thinking mode, or None.

    - Auto-thinking models (_AUTO_THINKING_MODELS: deepseek-reasoner, deepseek-r1, qwq):
      These models automatically return reasoning_content in API responses; sending
      extra_body would cause 400 because the API already enables thinking by default.
      Return None to avoid duplicate activation.
    - Opt-in models (_OPT_IN_THINKING_MODELS: deepseek-chat): Return the activation
      payload to explicitly enable thinking mode.
    - All other models: Return None (no thinking mode).
    """
    if _model_matches(model, _AUTO_THINKING_MODELS):
        return None
    return _get_opt_in_payload(model, _OPT_IN_THINKING_MODELS)


def resolve_fallback_litellm_wire_models(
    model: str,
    model_list: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    """Resolve all wire models reachable from a configured alias."""
    normalized_model = (model or "").strip()
    if not normalized_model:
        return []

    resolved: List[str] = []
    if model_list:
        for entry in model_list:
            if not isinstance(entry, dict):
                continue
            entry_model_name = str(entry.get("model_name", "") or "").strip()
            if not entry_model_name:
                entry_params = entry.get("litellm_params", {}) or {}
                entry_model_name = str(entry_params.get("model") or "").strip()
            if entry_model_name != normalized_model:
                continue

            entry_params = entry.get("litellm_params", {}) or {}
            wire_model = str(entry_params.get("model") or normalized_model).strip()
            if wire_model and wire_model not in resolved:
                resolved.append(wire_model)

    if not resolved:
        wire_model = resolve_litellm_wire_model(normalized_model, model_list)
        if wire_model and wire_model not in resolved:
            resolved.append(wire_model)
    return resolved


# ============================================================
# LLM Tool Adapter
# ============================================================


def _refine_parse_error(error: BaseException) -> None:
    """A response returned fine but did not parse; keep the outbound fact."""
    attempt = last_attempt.get()
    if attempt is not None and attempt.status == STATUS_OK:
        attempt.refine_status(STATUS_PARSE_ERROR)


# How often the cancellation watcher polls the run while a streamed model
# response is being consumed in the local event loop.
_STREAM_CANCEL_POLL_SECONDS = 0.05


def _provider_stream_usage_keys(usage: Any) -> Optional[Dict[str, Any]]:
    """The usage fields the provider actually sent, if any.

    Providers deliver usage either as a pydantic payload (top level) or a
    plain dict (current Kimi puts it on the choice); only explicitly sent
    keys count, never library defaults.
    """
    if usage is None:
        return None
    if isinstance(usage, dict):
        return dict(usage)
    fields = getattr(usage, "model_fields_set", None)
    if fields:
        # exclude_unset recurses into nested Pydantic details (cache and
        # reasoning breakdowns): hand-picking top-level fields would keep
        # those details as opaque objects the Mapping-only sanitizer drops.
        dumped = usage.model_dump(exclude_unset=True)
        return {key: dumped[key] for key in fields if key in dumped}
    return None


_GEMINI_USAGE_KEY_MAP = {
    "promptTokenCount": "prompt_token_count",
    "candidatesTokenCount": "candidates_token_count",
    "totalTokenCount": "total_token_count",
    "cachedContentTokenCount": "cached_content_token_count",
}


def _observe_native_chunk(chunk: Any, sink: Dict[str, Any]) -> None:
    """Read raw native (non-openai) SSE payloads for usage and finish.

    Anthropic and Gemini deliver usage and stop reasons inside their SSE
    events long before LiteLLM normalizes them (and LiteLLM synthesizes a
    stop when the stream ends without one, so the normalized chunk is never
    evidence). Payloads are dicts at the ``chunk_parser`` boundary; usage
    keys overwrite per event (never summed, never frozen at start).
    """
    if not isinstance(chunk, dict):
        return
    event_type = chunk.get("type") or ""
    if event_type == "message_start":
        usage = _provider_stream_usage_keys((chunk.get("message") or {}).get("usage"))
        if usage:
            sink["usage"] = {**(sink.get("usage") or {}), **usage}
        return
    if event_type == "message_delta":
        usage = _provider_stream_usage_keys(chunk.get("usage"))
        if usage:
            sink["usage"] = {**(sink.get("usage") or {}), **usage}
        stop_reason = (chunk.get("delta") or {}).get("stop_reason")
        if stop_reason:
            sink["finish"] = stop_reason
        return
    if event_type == "message_stop":
        # The terminal event is itself finish evidence.
        sink["finish"] = sink.get("finish") or "stop"
        return
    # Gemini: usageMetadata plus candidates[].finishReason, camelCase keys.
    # Thinking tokens are reported separately from visible candidates; they
    # are preserved as a completion-side reasoning detail so the provider's
    # own breakdown survives sanitization and normalization reads the
    # complete output (candidates + thoughts) without inventing zeros.
    usage_metadata = chunk.get("usageMetadata")
    if isinstance(usage_metadata, dict):
        gemini_usage: Dict[str, Any] = {}
        for key, value in usage_metadata.items():
            mapped = _GEMINI_USAGE_KEY_MAP.get(key)
            if mapped is not None:
                gemini_usage[mapped] = value
        if "thoughtsTokenCount" in usage_metadata:
            # Raw actual count straight into the nested detail: the existing
            # usage sanitizer owns the range/type boundary at adoption time.
            gemini_usage["completion_tokens_details"] = {
                "reasoning_tokens": usage_metadata["thoughtsTokenCount"]
            }
        sink["usage"] = {**(sink.get("usage") or {}), **gemini_usage}
    for candidate in chunk.get("candidates") or []:
        finish_reason = candidate.get("finishReason")
        if finish_reason:
            sink["finish"] = finish_reason


async def _observe_raw_stream(wrapper: Any, attempt: Any, sink: Dict[str, Any]) -> None:
    """Observe provider usage at the raw SDK stream boundary.

    Attaches to whatever the attempt captured at the transport boundary:
    the openai ``AsyncStream`` iterator (``_iterator``) for openai-compatible
    providers, or a single-shot read-only hook around the native
    ``chunk_parser`` for Anthropic/Gemini. Lazy litellm wrappers have their
    stream fetched inside this same constrained coroutine first. Without a
    captured wrapper (e.g. an unknown Router layout) the observation is
    skipped and provider usage stays honestly unknown.
    """
    if wrapper is None or not isinstance(wrapper, CustomStreamWrapper):
        return
    inner = getattr(wrapper, "completion_stream", None)
    if inner is None and getattr(wrapper, "make_call", None) is not None:
        inner = await wrapper.fetch_stream()
    if inner is None:
        return
    sink["observed"] = True

    if hasattr(inner, "_iterator"):
        source = inner._iterator
        owner = sink

        class _ObservingAsyncIter:
            def __init__(self) -> None:
                self._source = source

            def __aiter__(self) -> "_ObservingAsyncIter":
                return self

            async def __anext__(self) -> Any:
                chunk = await self._source.__anext__()
                top_level = _provider_stream_usage_keys(getattr(chunk, "usage", None))
                if top_level is not None:
                    owner["usage"] = top_level
                else:
                    # Some providers deliver the usage tail on the choice; the
                    # top level keeps priority when both exist.
                    choices = getattr(chunk, "choices", None) or []
                    if choices:
                        choice_usage = _provider_stream_usage_keys(getattr(choices[0], "usage", None))
                        if choice_usage is not None:
                            owner["usage"] = choice_usage
                # Stream finish lives on the chunk, not the response object.
                choices = getattr(chunk, "choices", None)
                finish = getattr(choices[0], "finish_reason", None) if choices else None
                if finish:
                    owner["finish"] = finish
                return chunk

        inner._iterator = _ObservingAsyncIter()
        return

    original_parser = getattr(inner, "chunk_parser", None)
    if callable(original_parser):
        owner = sink

        def _observing_parser(chunk: Any) -> Any:
            try:
                _observe_native_chunk(chunk, owner)
            except Exception:  # noqa: BLE001 - observation must not alter the stream
                pass
            result = original_parser(chunk)
            # LiteLLM synthesizes finish_reason "stop" for an Anthropic
            # message_delta whose stop_reason is null, and the streaming
            # wrapper stops iterating once it sees a finish reason. The
            # official protocol allows several message_delta events with
            # cumulative usage, so a null stop_reason is not a finish: clear
            # the synthesized reason so the SDK keeps reading to the real
            # terminal event. Nothing else in the parsed chunk is touched.
            if isinstance(chunk, dict) and chunk.get("type") == "message_delta":
                if not (chunk.get("delta") or {}).get("stop_reason"):
                    for choice in getattr(result, "choices", None) or []:
                        choice.finish_reason = None
            return result

        inner.chunk_parser = _observing_parser


class LLMToolAdapter:
    """Unified adapter for tool-calling via LiteLLM.

    Supports all providers (Gemini, Anthropic, OpenAI, DeepSeek, etc.) through
    a single litellm.completion() interface with optional Router for multi-key
    load balancing.
    """

    def __init__(self, config=None):
        config = config or get_config()
        self._config = config
        self._router = None  # litellm Router (multi-key primary model)
        self._legacy_router_model_list: List[Dict[str, Any]] = []
        self._litellm_available = False
        self._register_custom_model_pricing()
        self._init_litellm()

    @staticmethod
    def _register_custom_model_pricing() -> None:
        """Register custom model pricing for models not in LiteLLM's built-in price list.

        This prevents cost calculation errors for MiniMax-M2.7 and similar models.
        """
        for model_name, pricing in _CUSTOM_MODEL_PRICING.items():
            try:
                litellm.register_model({model_name: pricing})
                logger.debug(f"Registered custom pricing for {model_name}")
            except Exception as e:
                logger.debug(
                    f"Model {model_name} may already be registered or pricing error: {e}"
                )

    def _has_channel_config(self) -> bool:
        """Check if multi-channel config (channels / YAML) is active."""
        return bool(self._config.llm_model_list) and not all(
            e.get("model_name", "").startswith("__legacy_")
            for e in self._config.llm_model_list
        )

    def _init_litellm(self) -> None:
        """Initialize litellm Router from channels / YAML / legacy keys."""
        config = self._config
        self._legacy_router_model_list = []
        litellm_model = get_effective_agent_primary_model(config)
        if not litellm_model:
            logger.warning("Agent LLM: no effective primary model configured")
            return

        self._litellm_available = True

        # --- Channel / YAML path ---
        if self._has_channel_config():
            model_list = config.llm_model_list
            self._router = _StreamAttemptRouter(
                model_list=model_list,
                routing_strategy="simple-shuffle",
                num_retries=2,
            )
            unique_models = list(
                dict.fromkeys(e["litellm_params"]["model"] for e in model_list)
            )
            logger.info(
                f"Agent LLM: Router initialized from channels/YAML — "
                f"{len(model_list)} deployment(s), models: {unique_models}"
            )
            return

        # --- Legacy path ---
        keys = get_api_keys_for_model(litellm_model, config)
        if not keys:
            logger.info(
                f"Agent LLM: litellm initialized (model={litellm_model}, "
                f"API key from environment)"
            )
            return

        if len(keys) > 1:
            ep = extra_litellm_params(litellm_model, config)
            legacy_model_list = [
                {
                    "model_name": litellm_model,
                    "litellm_params": {
                        "model": litellm_model,
                        "api_key": k,
                        **ep,
                    },
                }
                for k in keys
            ]
            self._legacy_router_model_list = legacy_model_list
            self._router = _StreamAttemptRouter(
                model_list=legacy_model_list,
                routing_strategy="simple-shuffle",
                num_retries=2,
            )
            logger.info(
                f"Agent LLM: Legacy Router initialized with {len(keys)} keys "
                f"for {litellm_model}"
            )
        else:
            logger.info(f"Agent LLM: litellm initialized (model={litellm_model})")

    @property
    def is_available(self) -> bool:
        """True if litellm is configured and at least one API key is present."""
        return self._router is not None or self._litellm_available

    @property
    def primary_provider(self) -> str:
        """Provider name extracted from litellm_model prefix."""
        model = get_effective_agent_primary_model(self._config)
        if "/" in model:
            return model.split("/")[0]
        return model or "none"

    # ============================================================
    # Unified call
    # ============================================================

    def call_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[dict[str, Any]],
        provider: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> LLMResponse:
        """Send messages + tool declarations to LLM, return normalized response.

        Args:
            messages: Conversation history in provider-neutral format:
                      [{"role": "system"/"user"/"assistant"/"tool", "content": ...}, ...]
            tools: OpenAI-format tool declarations; litellm converts to each provider's format.
            provider: Ignored (kept for backward compatibility).

        Returns:
            LLMResponse with either content (final answer) or tool_calls.
        """
        return self.call_completion(
            messages, tools=tools, provider=provider, timeout=timeout,
        )

    def call_text(
        self,
        messages: List[Dict[str, Any]],
        *,
        provider: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> LLMResponse:
        """Send a text-only completion through the shared routing stack."""
        return self.call_completion(
            messages,
            tools=None,
            provider=provider,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )

    def call_completion(
        self,
        messages: List[Dict[str, Any]],
        *,
        tools: Optional[List[dict[str, Any]]] = None,
        provider: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> LLMResponse:
        """Shared completion path for both tool and text-only calls."""
        config = self._config
        models_to_try = get_effective_agent_models_to_try(config)
        if not models_to_try:
            error_msg = (
                "No LLM configured. Please set LITELLM_MODEL, LLM_CHANNELS, "
                "or provider API keys before using Agent."
            )
            logger.error(error_msg)
            return LLMResponse(content=error_msg, provider="error")
        started_at = time.monotonic()
        providers = [self._get_model_provider(model) for model in models_to_try]

        last_error: Optional[Exception] = None
        hit_rate_limit = False
        for idx, model in enumerate(models_to_try):
            remaining_timeout = timeout
            if timeout is not None:
                remaining_timeout = max(
                    0.0, float(timeout) - (time.monotonic() - started_at)
                )
                if remaining_timeout <= 0:
                    last_error = TimeoutError(
                        f"LLM completion timed out before trying fallback model {model}"
                    )
                    break
            try:
                return self._call_litellm_model(
                    messages,
                    tools or [],
                    model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=remaining_timeout,
                )
            except Exception as e:
                if isinstance(e, _resolve_litellm_exception("RateLimitError")):
                    logger.warning("Agent LLM rate-limited on %s: %s", model, e)
                    last_error = e
                    hit_rate_limit = True

                    # Avoid blind backoff across different providers; cross-provider
                    # fallback usually means different accounts/rate-limit buckets.
                    should_backoff = (
                        idx + 1 < len(models_to_try)
                        and providers[idx] == providers[idx + 1]
                    )
                    if should_backoff:
                        backoff_sleep = min(2.0, (time.monotonic() - started_at) * 0.1 + 0.5)
                        if timeout is not None:
                            remaining_timeout = max(
                                0.0, float(timeout) - (time.monotonic() - started_at)
                            )
                            if remaining_timeout > 0:
                                time.sleep(min(backoff_sleep, remaining_timeout))
                        else:
                            time.sleep(backoff_sleep)
                    continue
                if isinstance(
                    e, _resolve_litellm_exception("ContextWindowExceededError")
                ):
                    logger.warning(
                        "Agent LLM context window exceeded on %s: %s", model, e
                    )
                    last_error = e
                    continue
                logger.warning(
                    "Agent LLM call failed with %s: %s",
                    model, str(e) or type(e).__name__,
                )
                last_error = e
                continue

        suffix = " (rate-limit encountered during fallback)" if hit_rate_limit else ""
        # A bare asyncio.wait_for expiry raises TimeoutError whose str() is
        # empty; keep the type name visible so timeout stays recognisable at
        # every downstream text boundary. Non-empty messages pass unchanged.
        last_error_text = str(last_error) or type(last_error).__name__
        error_msg = f"All LLM models failed{suffix}. Last error: {last_error_text}"
        logger.error(error_msg)
        return LLMResponse(content=error_msg, provider="error")

    @staticmethod
    def _get_model_provider(model: str) -> str:
        """Return LiteLLM provider namespace for model fallback grouping."""
        if "/" in model:
            return model.split("/", 1)[0]
        return "openai"

    def _call_litellm_model(
        self,
        messages: List[Dict[str, Any]],
        tools: List[dict[str, Any]],
        model: str,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> LLMResponse:
        """Call a specific litellm model with OpenAI-format messages and tools."""
        openai_messages = self._convert_messages(messages, target_model=model)

        # Use short model name (without provider prefix) for thinking model lookup
        model_short = model.split("/")[-1] if "/" in model else model
        extra = get_thinking_extra_body(model_short)

        call_kwargs: Dict[str, Any] = {
            "model": model,
            "messages": openai_messages,
            # One streamed upstream response for Router, direct and fallback
            # calls alike; aggregated below into the original full-response
            # contract before the existing parser runs.
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if max_tokens is not None:
            call_kwargs["max_tokens"] = max_tokens
        if timeout is not None:
            call_kwargs["timeout"] = timeout
            # The caller owns the deadline; nested Router/SDK retries reset it.
            call_kwargs["num_retries"] = 0
            call_kwargs["max_retries"] = 0
            call_kwargs["disable_fallbacks"] = True

        if extra:
            call_kwargs["extra_body"] = extra

        if tools:
            call_kwargs["tools"] = tools

        # Use Router for primary model (multi-key), direct litellm for others
        use_channel_router = self._has_channel_config()
        _router_model_names = set(
            get_configured_llm_models(self._config.llm_model_list)
        )
        agent_primary_model = get_effective_agent_primary_model(self._config)
        uses_router = bool(
            use_channel_router and self._router and model in _router_model_names
        ) or bool(
            self._router and model == agent_primary_model and not use_channel_router
        )
        recovery_model_list = self._config.llm_model_list
        if self._router and model == agent_primary_model and not use_channel_router:
            recovery_model_list = (
                self._legacy_router_model_list or self._config.llm_model_list
            )
        if not uses_router:
            keys = get_api_keys_for_model(model, self._config)
            if keys:
                call_kwargs["api_key"] = keys[0]
            call_kwargs.update(extra_litellm_params(model, self._config))
        wire_models = resolve_fallback_litellm_wire_models(model, recovery_model_list)
        call_kwargs = apply_minimax_reasoning_params(
            call_kwargs,
            wire_models,
            self._config.minimax_reasoning_effort,
        )
        call_kwargs = apply_litellm_generation_params(
            call_kwargs,
            model,
            self._get_temperature() if temperature is None else temperature,
            model_list=recovery_model_list,
        )
        register_fallback_model_pricing(
            resolve_fallback_litellm_wire_models(model, self._config.llm_model_list)
        )
        if use_channel_router and self._router and model in _router_model_names:
            # Channel / YAML path: Router manages all models in its model_list
            acompletion_fn = cast(Router, self._router).acompletion
        elif self._router and model == agent_primary_model and not use_channel_router:
            # Legacy path: Router for primary model multi-key
            acompletion_fn = cast(Router, self._router).acompletion
        else:
            # Legacy/direct-env path: direct call (also handles direct-env
            # providers like groq/ or bedrock/ that are not in the Router
            # model_list even when channel mode is active)
            acompletion_fn = litellm.acompletion

        # One async admission opens, fully consumes and aggregates the stream
        # so the original parser below sees one complete response.
        response, stream_raw_usage = self._streamed_completion(
            acompletion_fn,
            call_kwargs,
            openai_messages,
            model=model,
            model_list=recovery_model_list,
            timeout=timeout,
        )

        try:
            return self._parse_litellm_response(
                response,
                model,
                openai_messages,
                model_list=recovery_model_list,
                stream_raw_usage=stream_raw_usage,
            )
        except Exception as e:
            # Transport succeeded (last_attempt is this attempt with its
            # usage already adopted); a parse failure is its terminal state.
            _refine_parse_error(e)
            raise

    def _get_temperature(self) -> float:
        """Return the raw configured temperature before per-model normalization."""
        return float(self._config.llm_temperature)

    def _streamed_completion(
        self,
        acompletion_fn: Any,
        call_kwargs: Dict[str, Any],
        messages: List[Dict[str, Any]],
        *,
        model: str,
        model_list: Optional[List[Dict[str, Any]]],
        timeout: Optional[float],
    ) -> Tuple[Any, Optional[Dict[str, Any]]]:
        """Open, fully consume and aggregate one streamed model response.

        One existing async admission (:func:`limited_acompletion`) owns the
        whole coroutine — awaiting the response, streaming every chunk and
        aggregating it — so the permit is held until an aggregated response
        exists, and the response-level adoption sees the real result. A
        local ``asyncio.wait_for`` scope (Python 3.10 compatible) covers
        header latency, first chunk, reasoning, body and end together with
        the caller's and the report's remaining budget; new chunks never
        extend it. A run cancellation propagates through the same scope: the
        read task actually exits, the transport is closed, and only then is
        the permit released. The sync-facing ContextVars are restored
        explicitly because asyncio Tasks copy but do not write back context.

        Returns the aggregated full response plus the usage keys the provider
        actually sent (``None`` when it sent none — LiteLLM's synthesized
        stream estimates must never pass as provider actuals).
        """
        run = current_research.get()
        # The single-response cap starts only once admitted (queue waiting
        # consumes the caller/report budget, never this cap).
        response_cap: Optional[float] = None
        if run is not None:
            from src.config import get_config

            response_cap = float(get_config().llm_stream_response_timeout)

        raw: Dict[str, Any] = {"usage": None, "finish": None, "observed": False}
        attempt_box: Dict[str, Any] = {}

        async def _admitted_call(**kwargs: Any) -> Any:
            """Open, consume and aggregate one streamed response.

            ``kwargs`` are the full SDK call parameters as handed back by the
            admission (``logger_fn`` observer attached, ``timeout`` reduced
            to the remaining caller/report budget) and go to the SDK as-is:
            the reduced timeout keeps serving as the first-chunk/idle read
            cap. The response wall clock starts here — admitted, past the
            queue — as the smaller of the configured response cap and that
            remaining budget, covering header latency, first chunk,
            reasoning, body and finish; new chunks never extend it. The
            permit is held until aggregation and transport close are done.
            """
            scope: Optional[float] = None
            remaining = kwargs.get("timeout")
            if isinstance(remaining, (int, float)):
                scope = max(float(remaining), 0.0)
            if response_cap is not None:
                scope = min(scope, response_cap) if scope is not None else response_cap
            # A mutable holder is not enough across the wait_for child task:
            # read the attempt through the ContextVar inside that task.
            attempt_box["attempt"] = current_attempt.get()

            async def _inner() -> Any:
                attempt = current_attempt.get()
                stream: Any = None
                from src.config import get_config

                # The first-chunk budget starts at the real outbound and is
                # shared by awaiting the response headers and receiving the
                # first chunk — a slow server cannot reset it by delivering
                # headers alone. Later chunks switch to the idle cap.
                first_deadline = monotonic() + float(get_config().llm_stream_first_chunk_timeout)
                idle_chunk_timeout = float(get_config().llm_stream_idle_chunk_timeout)
                chunks: List[Any] = []
                try:
                    stream = await asyncio.wait_for(
                        acompletion_fn(**kwargs),
                        timeout=max(first_deadline - monotonic(), 0.0),
                    )
                    # The raw wrapper is the Router-recorded stream_transport
                    # or the direct call's own litellm wrapper; without
                    # either, the observation degrades to honest unknown.
                    wrapper = getattr(attempt, "stream_transport", None)
                    if wrapper is None and isinstance(stream, CustomStreamWrapper):
                        wrapper = stream
                    await _observe_raw_stream(wrapper, attempt, raw)
                    # Manual iteration keeps the first-chunk and per-chunk
                    # idle caps in force for the whole read — including the
                    # usage tail after finish and native streams without any
                    # [DONE]. An expired cap cancels the blocked read, so the
                    # transport closes and the failure propagates as timeout.
                    aiter = stream.__aiter__()
                    chunk_deadline: Optional[float] = max(first_deadline - monotonic(), 0.0)
                    while True:
                        try:
                            chunk = await asyncio.wait_for(aiter.__anext__(), timeout=chunk_deadline)
                        except StopAsyncIteration:
                            break
                        chunk_deadline = idle_chunk_timeout
                        chunks.append(chunk)
                    return litellm.stream_chunk_builder(chunks, messages=messages)
                finally:
                    # Provider-actual usage reaches the attempt ledger before
                    # the response-level adoption and any parsing — on success
                    # and on failure alike, including a cancellation that
                    # lands before the response even opened.
                    from src.llm.concurrency import adopt_raw_stream_usage

                    adopt_raw_stream_usage(attempt, raw.get("usage"), observed=bool(raw.get("observed")))
                    if stream is not None:
                        aclose = getattr(stream, "aclose", None)
                        try:
                            if aclose is not None:
                                await aclose()
                            else:
                                # Some native wrappers expose no async close
                                # of their own; say so instead of claiming
                                # cleanup.
                                logger.debug(
                                    "stream type %s exposes no aclose; cleanup relies on the transport layer",
                                    type(stream).__name__,
                                )
                        except AttributeError:
                            logger.debug(
                                "stream type %s close unavailable on this SDK; cleanup relies on the transport layer",
                                type(stream).__name__,
                            )

            # Python 3.10 compatible wall-clock scope: wait_for cancels the
            # admitted coroutine on expiry, so a blocked read exits and the
            # transport closes before the failure propagates.
            return await asyncio.wait_for(_inner(), timeout=scope)

        async def _with_run_watch() -> Any:
            main_task = asyncio.current_task()
            watcher: Optional["asyncio.Task[None]"] = None
            if run is not None:
                async def _watch() -> None:
                    while True:
                        if run.stop_reason:
                            if main_task is not None:
                                main_task.cancel()
                            return
                        await asyncio.sleep(_STREAM_CANCEL_POLL_SECONDS)

                watcher = asyncio.create_task(_watch())
            try:
                return await _with_recovery()
            finally:
                if watcher is not None:
                    watcher.cancel()

        async def _with_recovery() -> Any:
            started = monotonic()
            try:
                # Full call kwargs flow through the admission: it owns the
                # queue deadline, attaches the per-attempt observer and
                # reduces the timeout before handing everything back to the
                # SDK call. Parameter recovery repeats the same admission
                # with the already-consumed original budget — never a reset.
                return await limited_acompletion(_admitted_call, **call_kwargs)
            except Exception as exc:
                recovery = classify_litellm_generation_param_error(exc)
                if recovery is None:
                    raise
                retry_kwargs = apply_litellm_param_recovery(dict(call_kwargs), recovery)
                if retry_kwargs == call_kwargs:
                    raise
                logger.warning(
                    "[LiteLLM] %s generation parameter rejected (%s), retrying once with request-scoped recovery",
                    model,
                    recovery.reason,
                )
                retry_budget: Optional[float] = None
                if isinstance(timeout, (int, float)):
                    retry_budget = float(timeout) - (monotonic() - started)
                    if retry_budget <= 0:
                        raise TimeoutError("LLM deadline exhausted before parameter recovery") from exc
                retry_call_kwargs = dict(retry_kwargs)
                if retry_budget is not None:
                    retry_call_kwargs["timeout"] = retry_budget
                # Only a recovered success is cached process-wide: a retry
                # that times out, is cancelled or is rejected again must not
                # poison every later call with this recovery.
                recovered = await limited_acompletion(_admitted_call, **retry_call_kwargs)
                remember_litellm_generation_param_recovery(
                    model,
                    recovery,
                    model_list=model_list,
                    request_overrides=retry_kwargs,
                )
                return recovered

        try:
            aggregated = asyncio.run(_with_run_watch())
        except asyncio.CancelledError:
            # The run's own cancellation watcher cancelled the task: keep the
            # existing ResearchStopped semantics for the fallback loop.
            if run is not None and run.stop_reason:
                raise ResearchStopped(run.reason_text) from None
            raise
        finally:
            attempt = attempt_box.get("attempt")
            if attempt is not None:
                attempt.stream_transport = None  # never retain a transport ref
                last_attempt.set(attempt)

        if raw.get("finish") is None:
            # A stream that ended without the provider's own finish evidence
            # (empty body, missing terminal chunk) was never completed by the
            # provider; LiteLLM synthesizing stop must not turn it into a
            # success. The attempt becomes a parse failure via the caller.
            _refine_parse_error(ValueError("model stream ended without finish evidence"))
            raise ValueError("model stream ended without finish evidence")
        if aggregated is None or not getattr(aggregated, "choices", None):
            _refine_parse_error(ValueError("model stream produced no aggregated response"))
            raise ValueError("model stream produced no aggregated response")
        return aggregated, raw.get("usage")

    def _convert_messages(
        self,
        messages: List[Dict[str, Any]],
        *,
        target_model: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Convert internal message format to OpenAI-compatible format for litellm."""
        openai_messages: List[Dict[str, Any]] = []
        target_provider = self._trace_provider_for_target(target_model)
        for msg in messages:
            trace_matches_target = _message_trace_matches_target(
                msg,
                target_model,
                target_provider=target_provider,
            )
            if not trace_matches_target:
                continue
            if msg["role"] == "tool":
                openai_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.get("tool_call_id", ""),
                        "content": msg["content"]
                        if isinstance(msg["content"], str)
                        else json.dumps(msg["content"]),
                    }
                )
            elif msg["role"] == "assistant" and msg.get("tool_calls"):
                openai_tc = []
                for tc in msg["tool_calls"]:
                    tc_dict: Dict[str, Any] = {
                        "id": tc.get("id", str(uuid.uuid4())[:8]),
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["arguments"]),
                        },
                    }
                    provider_specific_fields = dict(
                        tc.get("provider_specific_fields") or {}
                    )
                    sig = tc.get("thought_signature")
                    if sig is not None:
                        provider_specific_fields.setdefault("thought_signature", sig)
                    if provider_specific_fields:
                        tc_dict["provider_specific_fields"] = provider_specific_fields
                    openai_tc.append(tc_dict)
                content = (
                    msg.get("provider_blocks")
                    if msg.get("provider_blocks")
                    else msg.get("content")
                )
                openai_msg: Dict[str, Any] = {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": openai_tc,
                }
                if msg.get("reasoning_content") is not None:
                    openai_msg["reasoning_content"] = msg["reasoning_content"]
                openai_messages.append(openai_msg)
            else:
                openai_messages.append(
                    {
                        "role": msg["role"],
                        "content": msg["content"],
                    }
                )
        return openai_messages

    def _trace_provider_for_target(self, target_model: Optional[str]) -> str:
        if not target_model:
            return ""
        model_list = getattr(getattr(self, "_config", None), "llm_model_list", []) or []
        return resolved_provider_namespace(target_model, model_list)

    def _parse_litellm_response(
        self,
        response: Any,
        model: str,
        messages: Optional[List[Dict[str, Any]]] = None,
        *,
        model_list: Optional[List[Dict[str, Any]]] = None,
        stream_raw_usage: Any = _NON_STREAM_USAGE,
    ) -> LLMResponse:
        """Parse litellm OpenAI-compatible response into LLMResponse."""
        choice = response.choices[0]
        tool_calls: List[ToolCall] = []

        provider_blocks, provider_text = _extract_provider_blocks(choice)

        # Handle MiniMax-specific content_blocks format
        # MiniMax-M3 may return content_blocks at choice level or inside message
        # Check both possible locations for content_blocks to ensure consistency
        # Concatenate ALL text blocks to avoid truncating multi-block responses
        text_content = choice.message.content
        if isinstance(text_content, list):
            text_content = provider_text
        if text_content is None:
            text_content = provider_text

        # DeepSeek/Qwen thinking mode; not in standard OpenAI type, accessed via getattr
        reasoning_content = getattr(choice.message, "reasoning_content", None)

        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                # Tool calls are only executable once the arguments form a
                # complete JSON object. A truncated or non-object payload is
                # a parse failure of the whole response — never a call with
                # guessed arguments, and never a silent `{"raw": ...}` pass.
                raw_arguments = tc.function.arguments or ""
                if raw_arguments.strip():
                    try:
                        args: Dict[str, Any] = json.loads(raw_arguments)
                    except json.JSONDecodeError as exc:
                        raise ValueError(
                            f"model returned malformed tool arguments for "
                            f"{tc.function.name!r}: not valid JSON"
                        ) from exc
                    if not isinstance(args, dict):
                        raise ValueError(
                            f"model returned malformed tool arguments for "
                            f"{tc.function.name!r}: expected a JSON object"
                        )
                else:
                    args = {}

                provider_specific_fields = _provider_specific_fields_from(
                    getattr(tc, "provider_specific_fields", None)
                )
                provider_specific_fields.update(
                    _provider_specific_fields_from(
                        getattr(tc.function, "provider_specific_fields", None)
                    )
                )
                sig = provider_specific_fields.get("thought_signature")
                if sig is None:
                    sig = getattr(tc, "thought_signature", None)

                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=args,
                        thought_signature=sig,
                        provider_specific_fields=provider_specific_fields,
                    )
                )

        usage_model_list = (
            model_list
            if model_list is not None
            else getattr(getattr(self, "_config", None), "llm_model_list", []) or []
        )
        usage_model, provider_name = resolved_model_provider_identity(
            model, usage_model_list
        )
        if stream_raw_usage is _NON_STREAM_USAGE:
            usage_payload = extract_usage_payload(response)
        else:
            # Streamed responses: only the usage keys observed on the raw
            # provider chunks count. LiteLLM fills an aggregated stream
            # response with synthesized estimates when the provider sent no
            # tail, and those must never pass as provider actuals — absent
            # provider usage stays unknown.
            usage_payload = stream_raw_usage or None
        if usage_payload:
            usage = normalize_litellm_usage(
                usage_payload,
                model=usage_model or model,
                provider=provider_name,
            )
            usage = attach_message_hmacs(usage, messages)
        else:
            usage = {}
        return LLMResponse(
            content=text_content,
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
            provider_blocks=provider_blocks,
            usage=usage,
            provider=provider_name,
            model=model,
            raw=response,
            finish_reason=getattr(choice, "finish_reason", None),
        )


def register_fallback_model_pricing(models: Iterable[str]) -> None:
    """Register zero-cost pricing for unknown OpenAI-compatible models."""
    if not models:
        return
    register = getattr(litellm, "register_model", None)
    if not callable(register):
        return
    cost_map = getattr(litellm, "model_cost", {})
    if not isinstance(cost_map, dict):
        cost_map = {}
    for model in models:
        provider, wire_model = _split_provider_model(str(model))
        if provider != "openai":
            continue
        if not wire_model or wire_model.startswith("__legacy_"):
            continue
        custom_pricing = _CUSTOM_MODEL_PRICING.get(wire_model)
        if custom_pricing is not None:
            if wire_model in cost_map:
                continue
            try:
                register({wire_model: dict(custom_pricing)})
                logger.debug("Registered custom pricing for %s", wire_model)
            except Exception as exc:
                logger.debug(
                    "Custom pricing registration failed for %s, will try fallback pricing: %s",
                    wire_model,
                    exc,
                )
            else:
                continue
        if wire_model in cost_map or wire_model in _FALLBACK_MODEL_PRICING_REGISTERED:
            continue
        try:
            register({wire_model: dict(_FALLBACK_MODEL_PRICING)})
            _FALLBACK_MODEL_PRICING_REGISTERED.add(wire_model)
            logger.debug("Registered fallback pricing for %s", wire_model)
        except Exception as exc:
            logger.debug(
                "Fallback pricing registration skipped for %s: %s", wire_model, exc
            )
