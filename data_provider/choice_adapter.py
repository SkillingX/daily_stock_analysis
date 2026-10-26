"""Read Choice native data with one serialized, deadline-bounded SDK session.
The optional SDK is loaded only in an owned child; credentials never cross replies.
Report mapping stays in this adapter; native hangs terminate the owned process.
"""
from __future__ import annotations

import atexit
import importlib
import json
import logging
import math
import multiprocessing
import os
from contextvars import ContextVar
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from threading import Lock
from time import monotonic
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Literal, Annotated, NoReturn, TypeGuard, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError, Field, model_validator
from icontract import ensure

from data_provider.base import is_bse_code, normalize_stock_code
from data_provider.cross_source_validator import AnchorQuality, AnchorReading, SourceReadError, adopted_field_record

logger = logging.getLogger(__name__)

# 非研究入口也能用 call_id 关联：没有诊断上下文时日志仍要写出
choice_call_id: ContextVar[str] = ContextVar("choice_call_id", default="")

_EVIDENCE_TEXT_LIMIT = 300
_EVIDENCE_CHAIN_LIMIT = 4
_PROGRESS_INTERVAL_SECONDS = 1.0


def _known_secrets() -> tuple[str, ...]:
    return tuple(secret for secret in (os.getenv("CHOICE_USERNAME", ""), os.getenv("CHOICE_PASSWORD", "")) if secret.strip())


def redact_native_text(text: object, *, known_secrets: tuple[str, ...] = ()) -> str:
    """脱敏原生错误正文：先替换已知凭据值，再复用诊断脱敏，结果有界。"""
    sanitized = str(text or "")
    for secret in known_secrets:
        if secret:
            sanitized = sanitized.replace(secret, "[REDACTED]")
    from src.services.run_diagnostics import sanitize_diagnostic_text
    return sanitize_diagnostic_text(sanitized, max_length=_EVIDENCE_TEXT_LIMIT) or ""


class _Trace:
    def __init__(self) -> None:
        self.stage = "not_dispatched"
        self.dispatched = False
        self.reason = "ok"
        self.evidence: dict[str, object] = {}
        self.observed = ""
        self.call_id = ""
        self.method = ""
        self.indicators = ""
        self.deadline = 0.0
        self.started = 0.0
        self.lock_acquired_at: float | None = None
        self.dispatched_at: float | None = None
        self.trace_token: object = None
        self.id_token: object = None
        self.finished = False


_trace: ContextVar[_Trace | None] = ContextVar("choice_call_trace", default=None)
_defer_terminal: ContextVar[bool] = ContextVar("choice_defer_terminal", default=False)


def _safe_log(level: str, message: str, *args: object) -> None:
    try:
        getattr(logger, level)(message, *args)
    except Exception:
        return


def _remember(stage: str, reason: str | None = None, evidence: dict[str, object] | None = None, *, dispatched: bool | None = None, observed: str | None = None) -> None:
    trace = _trace.get()
    if trace is None:
        return
    trace.stage = stage
    if reason is not None:
        trace.reason = reason
    if evidence is not None:
        trace.evidence = evidence
    if dispatched is not None:
        trace.dispatched = dispatched
    if observed is not None:
        trace.observed = observed


_Parsed = TypeVar("_Parsed")


def _is_progress(payload: object) -> TypeGuard[dict[str, object]]:
    return isinstance(payload, dict) and set(payload) == {"stage"} and payload.get("stage") in {"waiting_login", "waiting_query"}


def _fail(reason: str, stage: str, evidence: dict[str, object] | None = None, cause: BaseException | None = None) -> NoReturn:
    _remember(stage, reason, evidence, dispatched=stage not in {"not_dispatched", "waiting_lock", "lock_wait_timeout"})
    if cause is None:
        raise SourceReadError("choice", reason)
    raise SourceReadError("choice", reason) from cause


def _scope_labels() -> tuple[str, str]:
    try:
        from src.core.research_run import current_research, research_dimension
        run = current_research.get()
        run_id = run.run_id if run is not None and run.run_id else "-"
        dimension = research_dimension.get() or "-"
    except Exception:
        return "-", "-"
    return run_id, dimension


def _durations(trace: _Trace, end: float) -> tuple[float, float]:
    if trace.dispatched_at is not None:
        return max(0.0, trace.dispatched_at - trace.started), max(0.0, end - trace.dispatched_at)
    return max(0.0, end - trace.started), 0.0


def _finish_call(trace: _Trace) -> None:
    if trace.finished:
        return
    trace.finished = True
    end = monotonic()
    wait_s, execute_s = _durations(trace, end)
    run_id, dimension = _scope_labels()
    evidence = trace.evidence
    text = evidence.get("error_text")
    if text is not None:
        text = redact_native_text(text, known_secrets=_known_secrets())
    level = "info" if trace.reason in {"ok", "cancelled"} else "warning"
    _safe_log(
        level,
        "Choice call terminal: call_id=%s run_id=%s method=%s indicators=%s dim=%s stage=%s observed=%s "
        "remaining_budget_s=%.1f elapsed_s=%.1f wait_s=%.1f execute_s=%.1f dispatched=%s reason=%s "
        "native_code=%s error_text=%s exception_chain=%s",
        trace.call_id, run_id, trace.method, trace.indicators, dimension, trace.stage, trace.observed or "-",
        trace.deadline - end, end - trace.started, wait_s, execute_s, trace.dispatched, trace.reason,
        evidence.get("native_code"), text, evidence.get("exception_chain"),
    )
    if trace.trace_token is not None:
        _trace.reset(trace.trace_token)  # type: ignore[arg-type]
        trace.trace_token = None
    if trace.id_token is not None:
        choice_call_id.reset(trace.id_token)  # type: ignore[arg-type]
        trace.id_token = None


def _exception_chain(exc: BaseException) -> tuple[str, ...]:
    """只保留异常类型链，不带消息（消息可能含凭据）。"""
    chain: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(chain) < _EVIDENCE_CHAIN_LIMIT:
        seen.add(id(current))
        chain.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    return tuple(chain)


def _native_error_evidence(code: int, message: str, *, known_secrets: tuple[str, ...]) -> dict[str, object]:
    return {"error": _native_error(code, message), "native_code": code,
            "error_text": redact_native_text(message, known_secrets=known_secrets)}


class NativeReply(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, validate_assignment=True, allow_inf_nan=False)
    error: str | None = None
    native_code: int | None = None
    # 有返回时保留脱敏后的原生错误正文与异常类型链；无返回时两者均为空
    error_text: str | None = None
    exception_chain: tuple[str, ...] = ()
    # 进度回执只用 waiting_*；终态回执携带产生它的 login / query，父进程不得按原因猜测
    stage: Literal["waiting_login", "waiting_query", "login", "query"] | None = None
    codes: list[str] = Field(default_factory=list)
    indicators: list[str] = Field(default_factory=list)
    dates: list[str] = Field(default_factory=list)
    data: dict[str, list[float | int | str | None | list[float | int | str | None]]] | list[str] = Field(default_factory=dict)


class AnnualReport(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, validate_assignment=True, allow_inf_nan=False)
    report_date: Annotated[str, Field(pattern=r"^\d{4}-12-31$")]
    disclosure_date: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    source: Literal["choice"] = "choice"
    currency: Literal["CNY"] = "CNY"
    statement_scope: Literal["consolidated"] = "consolidated"
    adjustment: Literal["unadjusted"] = "unadjusted"
    revenue: Decimal | None = None
    operating_cost: Decimal | None = None
    gross_profit: Decimal | None = None
    net_profit: Decimal | None = None
    roe: Decimal | None = None
    gross_margin: Decimal | None = None
    op_cash_flow: Decimal | None = None
    assets_total: Decimal | None = None
    total_debt: Decimal | None = None
    equity_total: Decimal | None = None
    eps: Decimal | None = None

    @model_validator(mode="after")
    def validate_dates(self) -> AnnualReport:
        report, disclosed = date.fromisoformat(self.report_date), date.fromisoformat(self.disclosure_date)
        if not report < disclosed <= date.today():
            raise ValueError("Annual disclosure must follow the report period and be known today")
        return self

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> AnnualReport:
        payload = {key: record[key] for key in cls.model_fields if key in record}
        payload.update({key: record.get(key) for key in ("source", "currency", "statement_scope", "adjustment")})
        report = cls.model_validate_json(json.dumps(payload))
        if record.get("year") != int(report.report_date[:4]):
            raise ValueError("Annual year and report period disagree")
        return report

    @ensure(lambda self, result: result["gross_profit"] is None or self.revenue is None or self.operating_cost is None or abs(Decimal(str(result["gross_profit"])) - (self.revenue-self.operating_cost)) <= Decimal("0.01"), "Gross profit must agree with revenue minus cost")
    def as_record(self) -> dict[str, Any]:
        record = self.model_dump(mode="json")
        for field in ("revenue", "operating_cost", "gross_profit", "net_profit", "roe", "gross_margin", "op_cash_flow", "assets_total", "total_debt", "equity_total", "eps"):
            value = getattr(self, field)
            record[field] = float(value) if value is not None else None
        if self.gross_profit is not None and self.revenue is not None and self.operating_cost is not None:
            computed = self.revenue-self.operating_cost
            if self.gross_profit is not None and abs(self.gross_profit-computed) > Decimal("0.01"):
                record["gross_profit"] = None
                record["errors"] = ["gross_profit:identity_mismatch"]
            else:
                record["gross_profit"] = float(computed)
        record["year"] = int(self.report_date[:4])
        record["period"] = self.report_date
        record["debt_ratio"] = float(self.total_debt/self.assets_total*100) if self.total_debt is not None and self.assets_total is not None and self.assets_total > 0 else None
        record["roe_method"] = "weighted_roe"
        record["field_meta"] = {field: {"source": self.source, "source_family": "eastmoney", "period": self.report_date, "period_basis": "annual", "disclosed_at": self.disclosure_date, "currency": self.currency, "statement_scope": self.statement_scope, "adjustment": self.adjustment, "unit": "percentage_point" if field in {"roe", "gross_margin", "debt_ratio"} else "CNY/share" if field == "eps" else "currency_base"} for field in ("revenue", "operating_cost", "gross_profit", "net_profit", "roe", "gross_margin", "op_cash_flow", "assets_total", "total_debt", "equity_total", "eps", "debt_ratio")}
        return record


def _native_error(code: int, message: str) -> str:
    if code in {10001001, 10001009, 10001011}:
        return "authentication_failed"
    text = message.lower()
    if any(word in text for word in ("login", "password", "token", "expired", "reactivate", "认证", "登录", "激活")):
        return "authentication_failed"
    if any(word in text for word in ("quota", "限额", "额度", "次数")):
        return "quota_exhausted"
    if any(word in text for word in ("permission", "无权限", "权限", "授权")):
        return "permission_denied"
    if any(word in text for word in ("timeout", "超时")):
        return "timeout"
    return "business_error"


def _serve_native(connection: Connection) -> None:
    with open(os.devnull, "w") as quiet:
        os.dup2(quiet.fileno(), 1)
        os.dup2(quiet.fileno(), 2)
    sdk: Any = None
    logged_in = False
    try:
        while True:
            request = connection.recv()
            if request is None:
                break
            method, args, options, seconds = request
            # 进度回执沿用原有 pipe，共享父进程给的同一绝对截止时间
            deadline = monotonic() + max(0.0, seconds)
            known_secrets = tuple(secret for secret in (os.getenv("CHOICE_USERNAME", ""), os.getenv("CHOICE_PASSWORD", "")) if secret.strip())
            progress = [0.0, ""]

            def report_stage(stage: str) -> None:
                # 阶段变化立即回执；同阶段重复回执按间隔节流
                if stage == progress[1] and monotonic() < progress[0]:
                    return
                try:
                    connection.send({"stage": stage})
                except (BrokenPipeError, OSError):
                    pass
                progress[0] = monotonic() + _PROGRESS_INTERVAL_SECONDS
                progress[1] = stage

            try:
                if not logged_in:
                    username = os.getenv("CHOICE_USERNAME", "")
                    password = os.getenv("CHOICE_PASSWORD", "")
                    if (username or password) and (
                        not username.strip() or not password.strip()
                        or any(char in username + password for char in ",\r\n\x00")
                    ):
                        connection.send({"error": "invalid_configuration", "stage": "login"})
                        continue
                    force_login = os.getenv("CHOICE_FORCE_LOGIN", "false").strip().lower() or "false"
                    if force_login not in {"true", "false", "1", "0"}:
                        connection.send({"error": "invalid_configuration", "stage": "login"})
                        continue
                    options_login = f"ForceLogin={int(force_login in {'true', '1'})},RecordLoginInfo=0,HTTPTimeout={max(1, int(seconds))}"
                    if username and password:
                        options_login += f",UserName={username},PassWord={password}"
                    if sdk is None:
                        sdk = importlib.import_module("EmQuantAPI").c
                    report_stage("waiting_login")
                    login = sdk.start(options_login, logcallback=lambda _: 1)
                    if login.ErrorCode:
                        connection.send({**_native_error_evidence(login.ErrorCode, login.ErrorMsg, known_secrets=known_secrets), "stage": "login"})
                        continue
                    logged_in = True
                if monotonic() >= deadline:
                    # 登录已耗尽共享截止时间，查询没有新预算
                    connection.send({"error": "login_timeout", "stage": "login"})
                    continue
                report_stage("waiting_query")
                result = getattr(sdk, method)(*args, options)
                if result.ErrorCode:
                    connection.send({**_native_error_evidence(result.ErrorCode, result.ErrorMsg, known_secrets=known_secrets), "stage": "query"})
                else:
                    connection.send({"codes": result.Codes, "indicators": result.Indicators,
                                     "dates": [str(d) for d in result.Dates], "data": result.Data, "stage": "query"})
            except (ImportError, OSError) as exc:
                connection.send({"error": "sdk_unavailable", "stage": "query" if progress[1] == "waiting_query" else "login",
                                 "exception_chain": _exception_chain(exc)})
            except Exception as exc:
                connection.send({"error": "invalid_response", "stage": "query" if progress[1] == "waiting_query" else "login",
                                 "error_text": redact_native_text(exc, known_secrets=known_secrets),
                                 "exception_chain": _exception_chain(exc)})
    except (EOFError, BrokenPipeError):
        pass
    finally:
        if logged_in:
            sdk.stop()
        connection.close()


class ChoiceAdapter:
    name = "choice"
    contract_version = "choice-v4"
    _lock = Lock()
    _process: BaseProcess | None = None
    _connection: Connection | None = None

    @staticmethod
    def completed_day() -> date:
        """Daily amounts/valuation use closed sessions, not today's partial session."""
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        return now.date() if now.time() >= time(15) else now.date() - timedelta(days=1)

    @classmethod
    def close(cls) -> None:
        process, connection = cls._process, cls._connection
        cls._process, cls._connection = None, None
        if connection is not None:
            try:
                connection.send(None)
            except (BrokenPipeError, OSError):
                pass
            try:
                connection.close()
            except OSError:
                pass
        if process is None:
            return
        try:
            process.join(timeout=0.2)
            if process.is_alive():
                process.terminate()
            process.join(timeout=0.1)
            if process.is_alive():
                process.kill()
                process.join(timeout=0.1)
            process.close()
        except (OSError, ValueError):
            if process.is_alive():
                try:
                    process.kill()
                    process.join(timeout=0.1)
                except (OSError, ValueError):
                    return

    @classmethod
    def _native_request(cls, method: Literal["css", "csd", "tradedates"], args: tuple[str, ...], options: str, deadline: float) -> object:
        from src.core.research_run import current_research, ResearchStopped
        run = current_research.get()
        started = monotonic()
        stage = "waiting_lock"
        sent = False
        _remember(stage)
        try:
            while True:
                if run is not None:
                    run.check()
                remaining = deadline - monotonic()
                if remaining <= 0:
                    _fail("lock_wait_timeout", "lock_wait_timeout")
                if cls._lock.acquire(timeout=min(0.05, remaining) if run is not None else remaining):
                    held = _trace.get()
                    if held is not None and held.lock_acquired_at is None:
                        held.lock_acquired_at = monotonic()
                    break
            try:
                if run is not None:
                    run.check()
                if deadline <= monotonic():
                    _fail("lock_wait_timeout", "lock_wait_timeout")
                if cls._process is None or not cls._process.is_alive():
                    cls.close()
                    context = multiprocessing.get_context("spawn")
                    parent, child = context.Pipe()
                    cls._process = context.Process(target=_serve_native, args=(child,), daemon=True, name="choice-native")
                    cls._connection = parent
                    cls._process.start()
                    child.close()
                assert cls._connection is not None
                if run is not None:
                    run.check()
                if deadline <= monotonic():
                    _fail("lock_wait_timeout", "lock_wait_timeout")
                stage = "dispatched"
                _remember(stage, dispatched=True)
                cls._connection.send((method, args, options, max(0.0, deadline - monotonic())))
                sent = True
                held = _trace.get()
                if held is not None:
                    held.dispatched_at = monotonic()
                while True:
                    if run is not None:
                        run.check()
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        break
                    if cls._connection.poll(min(0.1, remaining)):
                        payload: object = cls._connection.recv()
                        if _is_progress(payload):
                            stage = str(payload["stage"])
                            _remember(stage, observed=stage)
                            _safe_log(
                                "info", "Choice call progress: call_id=%s stage=%s elapsed_s=%.1f",
                                choice_call_id.get() or "-", stage, monotonic() - started,
                            )
                            continue
                        if isinstance(payload, dict) and payload.get("error") == "authentication_failed":
                            cls.close()
                        return payload
                while cls._connection.poll(0):
                    pending: object = cls._connection.recv()
                    if _is_progress(pending):
                        stage = str(pending["stage"])
                        _remember(stage, observed=stage)
                    elif isinstance(pending, dict):
                        return pending
                if stage == "waiting_login":
                    reason, terminal = "login_timeout", "waiting_login"
                elif stage == "waiting_query":
                    reason, terminal = "query_timeout", "waiting_query"
                else:
                    reason, terminal = "timeout", "no_response"
                cls.close()
                _fail(reason, terminal, {"error_text": "未收到响应"} if terminal == "no_response" else None)
            except ResearchStopped:
                if sent:
                    cls.close()
                    _remember("cancelled", "cancelled", observed=stage if stage.startswith("waiting_") else "", dispatched=True)
                raise
            except SourceReadError:
                raise
            except (EOFError, BrokenPipeError, OSError) as exc:
                cls.close()
                _fail("source_error", "no_response", {"exception_chain": _exception_chain(exc), "error_text": "未收到响应"}, exc)
            finally:
                cls._lock.release()
        except ResearchStopped:
            raise

    def query(self, method: Literal["css", "csd", "tradedates"], args: tuple[str, ...], options: str, *, deadline: float) -> NativeReply:
        from src.core.research_run import current_research, ResearchStopped
        trace = _Trace()
        trace.call_id = uuid4().hex[:12]
        trace.method = method
        trace.indicators = args[1] if len(args) > 1 else ""
        trace.deadline = deadline
        trace.started = monotonic()
        trace.trace_token = _trace.set(trace)
        trace.id_token = choice_call_id.set(trace.call_id)
        run_id, dimension = _scope_labels()
        try:
            _safe_log(
                "info",
                "Choice call start: call_id=%s run_id=%s method=%s indicators=%s dim=%s initial_budget_s=%.1f wait_s=0.0 execute_s=0.0",
                trace.call_id, run_id, method, trace.indicators, dimension, deadline - trace.started,
            )
            if run := current_research.get():
                run.check()
            remaining = int(deadline - monotonic())
            if remaining < 1:
                _remember("not_dispatched", "budget_exhausted", dispatched=False)
                raise SourceReadError(self.name, "budget_exhausted")
            raw = self._native_request(method, args, f"{options},RECVtimeout={remaining}".strip(","), deadline)
            try:
                reply = NativeReply.model_validate(raw)
            except ValidationError as exc:
                _remember("parse", "invalid_response", {"exception_chain": _exception_chain(exc)}, dispatched=True)
                raise SourceReadError(self.name, "invalid_response") from exc
            if reply.error:
                stage = reply.stage if reply.stage in {"login", "query"} else "unspecified"
                _remember(stage, reply.error, {
                    "native_code": reply.native_code,
                    "error_text": reply.error_text,
                    "exception_chain": reply.exception_chain,
                }, dispatched=True)
                raise SourceReadError(self.name, reply.error)
            if not _defer_terminal.get():
                _remember("completed", "ok", dispatched=True)
            return reply
        except ResearchStopped:
            if trace.reason != "cancelled":
                observed = trace.observed or (trace.stage if trace.stage.startswith("waiting_") else "")
                _remember("cancelled", "cancelled", observed=observed, dispatched=trace.dispatched)
            raise
        finally:
            if not _defer_terminal.get():
                _finish_call(trace)

    @staticmethod
    def stock_code(code: str) -> str:
        code = normalize_stock_code(code)
        if not code.isdigit() or len(code) != 6:
            raise ValueError("Choice integration currently requires a six-digit A-share code")
        return f"{code}.{'BJ' if is_bse_code(code) else 'SH' if code.startswith(('6', '9')) else 'SZ'}"

    def trading_dates(self, start: date, end: date, *, deadline: float) -> list[str]:
        def accept(reply: NativeReply) -> list[str]:
            if not isinstance(reply.data, list):
                raise SourceReadError(self.name, "invalid_response")
            dates = sorted({self.report_date(d) for d in reply.data if d})
            if any(d < start.isoformat() or d > end.isoformat() for d in dates):
                raise SourceReadError(self.name, "invalid_response")
            return dates
        return self._parsed("tradedates", (start.isoformat(), end.isoformat()), "Market=CNSESH", deadline, accept)

    def capital_series(self, code: str, start: date, end: date, *, deadline: float) -> list[tuple[str, float | None]]:
        def accept(reply: NativeReply) -> list[tuple[str, float | None]]:
            if reply.indicators != ["NETINFLOW"] or not isinstance(reply.data, dict):
                raise SourceReadError(self.name, "invalid_response")
            rows = reply.data.get(self.stock_code(code))
            if rows is None or len(rows) != 1 or not isinstance(rows[0], list) or len(rows[0]) != len(reply.dates):
                raise SourceReadError(self.name, "invalid_response")
            values: dict[str, float | None] = {}
            for raw_date, raw_value in zip(reply.dates, rows[0]):
                day = self.report_date(raw_date)
                if day < start.isoformat() or day > end.isoformat():
                    raise SourceReadError(self.name, "invalid_response")
                value = self.number(raw_value)
                if day in values and values[day] != value:
                    raise SourceReadError(self.name, "invalid_response")
                values[day] = value
            return sorted(values.items(), reverse=True)
        return self._parsed("csd", (self.stock_code(code), "NETINFLOW", start.isoformat(), end.isoformat()),
                            "Period=1,AdjustFlag=1,CurType=1,Order=1,Market=CNSESH", deadline, accept)

    @staticmethod
    def report_date(value: object) -> str:
        parts = str(value).strip().replace("/", "-").split("-")
        try:
            return date(*(int(part) for part in parts)).isoformat()
        except (ValueError, TypeError) as exc:
            raise SourceReadError("choice", "invalid_response") from exc

    def css(self, code: str, indicators: str, options: str, *, deadline: float) -> dict[str, object]:
        requested = self.stock_code(code)
        def accept(reply: NativeReply) -> dict[str, object]:
            if not isinstance(reply.data, dict):
                raise SourceReadError(self.name, "invalid_response")
            values = reply.data.get(requested)
            if values is None or len(values) != len(reply.indicators) or reply.indicators != indicators.split(","):
                raise SourceReadError(self.name, "invalid_response")
            return dict(zip(reply.indicators, values))
        return self._parsed("css", (requested, indicators), options, deadline, accept)

    def _parsed(self, method: Literal["css", "csd", "tradedates"], args: tuple[str, ...], options: str, deadline: float, accept: Callable[[NativeReply], _Parsed]) -> _Parsed:
        token = _defer_terminal.set(True)
        try:
            reply = self.query(method, args, options, deadline=deadline)
            try:
                result = accept(reply)
            except BaseException as exc:
                current = _trace.get()
                if current is not None and current.reason == "ok":
                    reason = exc.reason_code if isinstance(exc, SourceReadError) else "invalid_response"
                    _remember("parse", reason, {"exception_chain": _exception_chain(exc)})
                raise
            current = _trace.get()
            if current is not None and current.reason == "ok":
                _remember("completed", "ok", dispatched=True)
            return result
        finally:
            current = _trace.get()
            if current is not None:
                _finish_call(current)
            _defer_terminal.reset(token)

    @staticmethod
    def number(value: object) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise SourceReadError("choice", "invalid_response")
        try:
            number = float(Decimal(str(value)))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise SourceReadError("choice", "invalid_response") from exc
        if not math.isfinite(number):
            raise SourceReadError("choice", "invalid_response")
        return number

    def _record_failure(self, out: dict[str, Any], scope: str, exc: SourceReadError) -> None:
        """预算耗尽未分派只记阶段原因，不伪装成供应商连接故障。"""
        code = exc.reason_code
        if code in {"budget_exhausted", "lock_wait_timeout", "login_timeout", "query_timeout"}:
            out["errors"].append(f"choice:{scope}:{code}")
        else:
            out["errors"].append(str(exc))

    def snapshot(self, code: str, *, deadline: float, omit_indicators: tuple[str, ...] = (), include_optional: bool = True) -> dict[str, Any]:
        today = date.today()
        out: dict[str, Any] = {"growth": {}, "valuation": {}, "field_meta": {}, "errors": [], "contract": self.contract_version}
        omitted = set(omit_indicators)
        field_errors: dict[str, str] = {}
        from src.core.research_run import ResearchStopped
        report, disclosure, trade_day = None, None, None
        try:
            try:
                header = self.css(code, "NAME,LATESTREPORTNEW", f"EndDate={today}", deadline=deadline)
                out["stock_name"] = header.get("NAME")
                if header.get("LATESTREPORTNEW"):
                    report = self.report_date(header["LATESTREPORTNEW"])
                else:
                    out["errors"].append("choice:period_unknown")
            except SourceReadError as exc:
                self._record_failure(out, "header", exc)
                field_errors["growth"] = exc.reason_code
            if report is not None:
                growth_indicators = [
                    name for name in (
                        "OPERATEREVENEW", "PARENTNETPROFITNEW", "ROEWA", "GPMARGIN",
                        "YOYOR", "YOYPNI", "ACTUALISSUINGDATE",
                    )
                    if name not in omitted or name == "ACTUALISSUINGDATE"
                ]
                if any(name not in omitted for name in growth_indicators if name != "ACTUALISSUINGDATE"):
                    try:
                        fields = self.css(code, ",".join(growth_indicators), f"ReportDate={report},Type=1,CurType=2,DataAdjustType=2", deadline=deadline)
                        if fields.get("ACTUALISSUINGDATE"):
                            disclosure = self.report_date(fields["ACTUALISSUINGDATE"])
                        for field, indicator, caliber in (("roe", "ROEWA", "weighted_roe"), ("gross_margin", "GPMARGIN", "gross_margin"), ("revenue_yoy", "YOYOR", "operating_revenue_yoy"), ("net_profit_yoy", "YOYPNI", "parent_net_profit_yoy")):
                            if indicator in omitted:
                                continue
                            self._adopt(out, "growth", field, fields.get(indicator), "percentage_point", caliber, report, disclosure, "restated", indicator,
                                        None if disclosure and disclosure <= today.isoformat() and report <= today.isoformat() else "disclosure_unknown")
                        for field, indicator, caliber in (("revenue", "OPERATEREVENEW", "operating_revenue"), ("net_profit", "PARENTNETPROFITNEW", "parent_net_profit")):
                            if indicator in omitted:
                                continue
                            self._adopt(out, "growth", field, fields.get(indicator), "currency_base", caliber, report, disclosure, "restated", indicator,
                                        None if disclosure and disclosure <= today.isoformat() and report <= today.isoformat() else "disclosure_unknown")
                        out["growth"].update(report_date=report, disclosure_date=disclosure)
                    except SourceReadError as exc:
                        self._record_failure(out, "growth", exc)
                        field_errors["growth"] = exc.reason_code
                try:
                    balance = self.css(code, "SUMASSETNEW,SUMLIABNEW,ACTUALISSUINGDATE", f"ReportDate={report},Type=1,CurType=2,DataAdjustType=2", deadline=deadline)
                    disclosed = self.report_date(balance["ACTUALISSUINGDATE"]) if balance.get("ACTUALISSUINGDATE") else None
                    if disclosed and report < disclosed <= today.isoformat():
                        out["growth"]["balance_sheet"] = {"assets": self.number(balance.get("SUMASSETNEW")), "liabilities": self.number(balance.get("SUMLIABNEW")), "period": report, "disclosed_at": disclosed, "source": "choice", "currency": "CNY", "unit": "currency_base", "statement_scope": "consolidated", "adjustment": "restated"}
                    else:
                        out["errors"].append("choice:balance_sheet:disclosure_unknown")
                except SourceReadError as exc:
                    out["errors"].append(f"choice:balance_sheet:{exc.reason_code}")
            try:
                end = self.completed_day()
                dates = self.trading_dates(end - timedelta(days=14), end, deadline=deadline)
                if not dates:
                    raise SourceReadError(self.name, "calendar_unavailable")
                trade_day = dates[-1]
                fields = self.css(code, "PETTM,PB,MV", f"TradeDate={trade_day},EndDate={trade_day},type=8,AdjustFlag=1", deadline=deadline)
                for field, indicator, caliber in (("pe_ratio", "PETTM", "TTM"), ("pb_ratio", "PB", "MRQ"), ("total_mv", "MV", "total_market_cap")):
                    self._adopt(out, "valuation", field, fields.get(indicator), "currency_base" if field == "total_mv" else "multiple", caliber, None, trade_day, "unadjusted", indicator)
                out["valuation"]["trade_date"] = trade_day
            except SourceReadError as exc:
                self._record_failure(out, "valuation", exc)
                field_errors["valuation"] = exc.reason_code
            out["research"] = {"source": "choice", "as_of": today.isoformat(), "errors": [], "target_window_days": 180}
            try:
                out["research"]["targets"] = self.css(code, "RATINGTARGETPRICE,RATINGTARGETPRICEMIN,RATINGTARGETPRICEMAX", f"EndDate={today},Cycle=180", deadline=deadline)
            except SourceReadError as exc:
                out["research"]["errors"].append(f"choice:targets:{exc.reason_code}")
        except ResearchStopped:
            # A stopped run may still render completed, validated source facts.
            pass
        for block, fields in {"growth": ("revenue", "net_profit", "roe", "gross_margin", "revenue_yoy", "net_profit_yoy"), "valuation": ("pe_ratio", "pb_ratio", "total_mv")}.items():
            if block in field_errors:
                for field in fields:
                    out["field_meta"].setdefault(field, {
                        "quality": {"status": "missing", "reason_codes": ["source_error"]},
                        "source_errors": [{"source": self.name, "reason_code": field_errors[block]}],
                    })
        out.update(as_of=trade_day, report_date=report, disclosure_date=disclosure)
        out.setdefault("research", {"source": "choice", "as_of": today.isoformat(), "errors": []})
        if include_optional:
            self.append_optional_research(out, code, deadline=deadline, omit_indicators=omit_indicators)
        return out

    def append_optional_research(self, out: dict[str, Any], code: str, *, deadline: float, omit_indicators: tuple[str, ...] = ()) -> None:
        """行业/公司/股东在必要目标价之后补；调用方可先用 iFinD 结果决定 omit。"""
        from src.core.research_run import ResearchStopped
        today = date.today()
        research = out.setdefault("research", {"source": "choice", "as_of": today.isoformat(), "errors": []})
        research.setdefault("errors", [])
        omitted = set(omit_indicators)
        report = out.get("report_date")
        disclosure = out.get("disclosure_date")
        try:
            for name, indicators, options in (
                ("industry", "HISSW2021", f"EndDate={today},ClassiFication=1"),
                ("company", ",".join(name for name in ("MAINBUSINESS", "MAINPRODUCT", "COMPROFILE", "AHOLDER", "AHOLDERTYPE") if name not in omitted), f"EndDate={today}"),
            ):
                if not indicators:
                    continue
                try:
                    research[name] = self.css(code, indicators, options, deadline=deadline)
                except SourceReadError as exc:
                    research["errors"].append(f"choice:{name}:{exc.reason_code}")
            if report and disclosure and "HOLDNAMER" not in omitted:
                try:
                    holders = self.css(code, "HOLDNAMER,HOLDPCTR", f"ReportDate={report},Rank=11", deadline=deadline)
                    research["holders"] = {**holders, "period": report, "disclosed_at": disclosure,
                                           "note": "HOLDPCTR为前十名合计持股比例（%），不可分配为各股东比例"}
                except SourceReadError as exc:
                    research["errors"].append(f"choice:holders:{exc.reason_code}")
        except ResearchStopped:
            pass

    def annual_reports(self, code: str, limit: int = 4, *, deadline: float) -> dict[str, Any]:
        from src.core.research_run import ResearchStopped
        if not 1 <= limit <= 4:
            raise ValueError("Annual reports limit must be between one and four")
        today = date.today()
        out: dict[str, Any] = {"years": [], "valuation": {}, "as_of": today.isoformat(), "source": self.name, "data_contract": self.contract_version, "errors": []}
        mapping = {"revenue": "OPERATEREVENEW", "operating_cost": "OPERATEEXPNEW", "net_profit": "PARENTNETPROFITNEW", "gross_profit": "GROSSMARGIN", "roe": "ROEWA", "gross_margin": "GPMARGIN", "op_cash_flow": "NETOPERATECASHFLOWNEW", "assets_total": "SUMASSETNEW", "total_debt": "SUMLIABNEW", "equity_total": "SUMPARENTEQUITYNEW", "eps": "BASICEPSNEW"}
        for year in range(today.year-1, today.year-limit-2, -1):
            if len(out["years"]) == limit or monotonic() >= deadline:
                break
            report_date = f"{year}-12-31"
            try:
                values = self.css(code, ",".join((*mapping.values(), "ACTUALISSUINGDATE")), f"ReportDate={report_date},Type=1,CurType=2,DataAdjustType=1", deadline=deadline)
                if not values.get("ACTUALISSUINGDATE"):
                    out["errors"].append(f"choice:{report_date}:disclosure_unknown")
                    continue
                disclosure = self.report_date(values["ACTUALISSUINGDATE"])
                if disclosure > today.isoformat() or disclosure <= report_date:
                    out["errors"].append(f"choice:{report_date}:not_disclosed")
                    continue
                numbers: dict[str, Decimal | None] = {}
                for field, indicator in mapping.items():
                    try:
                        value = self.number(values.get(indicator))
                        numbers[field] = Decimal(str(values[indicator])) if value is not None else None
                    except SourceReadError as error:
                        numbers[field] = None
                        out["errors"].append(f"choice:{report_date}:{field}:{error.reason_code}")
                row = AnnualReport(report_date=report_date, disclosure_date=disclosure, **numbers).as_record()
                out["years"].append(row)
                out["errors"].extend(f"choice:{report_date}:{reason}" for reason in row.get("errors") or [])
            except ResearchStopped:
                break
            except SourceReadError as error:
                out["errors"].append(f"choice:{report_date}:{error.reason_code}")
                if error.reason_code in {"sdk_unavailable", "authentication_failed", "quota_exhausted", "timeout", "login_timeout", "query_timeout"}:
                    break
        from src.core.research_run import current_research
        run = current_research.get()
        out["status"] = "ok" if not (run and (run.stop_reason or run.finalized or run.remaining() <= 0)) and len(out["years"]) == limit and not out["errors"] and all(row.get(field) is not None for row in out["years"] for field in mapping) else "partial"
        return out

    def _adopt(self, out: dict[str, Any], block: str, field: str, value: object, unit: Literal["currency_base", "percentage_point", "multiple"], caliber: str, period: str | None, observed: str | None, adjustment: str, indicator: str, error: str | None = None) -> None:
        try:
            number = self.number(value)
        except SourceReadError as exc:
            out["errors"].append(str(exc))
            number = None
        if number is None:
            out["field_meta"][field] = {"quality": {"status": "missing", "reason_codes": ["null_value"]}}
            return
        reading = AnchorReading(self.name, number, caliber=caliber, period=period, period_basis="annual" if period and period.endswith("12-31") else "YTD" if period else "unknown", observed_at=observed,
                                fetched_at=datetime.now(timezone.utc).isoformat(), unit=unit, currency="CNY" if unit == "currency_base" else None,
                                raw_value=str(value), raw_label=indicator, normalization_error=error,
                                statement_scope="consolidated", adjustment=adjustment, disclosed_at=observed if block == "growth" else None)
        out[block][field] = number
        out["field_meta"][field] = adopted_field_record(field, reading, AnchorQuality(status="single_source", reason_codes=("single_family",)))


atexit.register(ChoiceAdapter.close)
