# 模型尝试计量链与用量归集（issue #55 阶段一）

本文定义深度投研报告中模型调用的归因、用量记录与完整性语义。实施背景见 [GitHub Issue #53](https://github.com/SkillingX/daily_stock_analysis/issues/53) 与施工票 #55；本文描述已落地行为。

## 范围

- 覆盖现有深研报告入口（双轨引擎）内的全部模型出站：研究员探索循环、维度叙述、六维补充评分、终读结论与经理成文。
- 不改变分析轮数、报告准入、兼容恢复与备用模型触发策略、超时与并发配置；本链路只做如实计量。

## 模型尝试账本（ModelAttempt）

每次真实准入尝试（`src/llm/concurrency.py` 的 `limited_completion` / `limited_acompletion`）在当前 `ResearchRun` 上记录一条 `ModelAttempt`（`src/llm/attempt.py`）。尝试在进入传输时即入账本（状态 `in_flight`，出站尚无证据），响应或失败在**同一对象**上覆盖更新，因此取消时的 partial_ready 快照已经可见在途尝试，而不会被"尚未返回"误记为成功。`outbound` 只由传输层证据置位：本地校验失败（请求从未交给传输层）保持 `False`，实际出站数不虚增。

| 字段 | 语义 |
| --- | --- |
| `attempt_id` / `run_id` | 尝试与所属研究运行 |
| `entry` / `purpose` | 业务入口与目的（见下表） |
| `dim` | 归因维度（来自 `research_dimension`） |
| `target_model` | 调用方配置的目标模型 |
| `wire_model` | 实际出站模型；来源与优先级见下节「wire_model 归因」。未出站或 Router 未选择时为 `None`（未知），不伪造、不用配置 `target_model` 补造 |
| `outbound` | 请求真实交给供应商传输层：per-call logger_fn 的 pre_api_call 事件（请求交给 SDK 传输层的交界，在途快照此时即可见出站）或 post_call/返回响应对象（携带原始用量证据）。本地校验失败（从未交给 SDK，如未选 provider）不触发这些事件，为 `False`——准入不等于出站 |
| `transport_observed` | 同上传输证据口径；不由异常类名推断 |
| `status` | 终态（见下表） |
| `usage_presence` | `provided` / `absent` / `unset`：归一化前的供应商 usage 存在性 |
| `usage_provided` | 供应商真实提供的 usage 键（白名单过滤）；缺失键保持缺失 |
| `usage_record_id` | `llm_usage` 审计行 ID（该尝试 usage 已持久化时）；不伪造 |
| `queue_seconds` / `exec_seconds` | 出站前等待 / 出站后执行耗时 |
| `finish_reason` | 供应商返回的完成原因 |

业务入口（`entry`）：

| entry | 调用点 |
| --- | --- |
| `agent` | 研究员 ReAct 循环（`run_agent_loop`） |
| `narrate` | 维度叙述单轮调用 |
| `scoring` | 六维补充评分 |
| `final_conclusion` | 终读结论 |
| `manager_writeup` | 经理终稿三波 |

终态分类：

| status | 含义 | 出站 |
| --- | --- | --- |
| `in_flight` | 已进入传输，响应未返回（快照中间态，不是成功） | 尝试中 |
| `ok` | 供应商正常返回 | 是 |
| `truncated` | 返回但 `finish_reason` 为 `length`/`content_filter` | 是 |
| `parse_error` | 返回后解析失败（`_parse_litellm_response` 抛错） | 是 |
| `timeout` | 实际请求/读取超时 | 是 |
| `rejected` | 供应商拒绝（限流/鉴权/权限） | 是 |
| `provider_error` | 其他供应商/传输错误（含 LiteLLM 本地校验失败，此时 `outbound=False`；流中途读失败经 LiteLLM 包装为 ServiceUnavailableError 族亦归此） | 见 `outbound` |
| `cancelled_in_flight` | 已出站后运行被取消，响应仍返回并保留 usage | 是 |
| `queue_timeout` | 等待名额超时，从未出站 | 否 |
| `stopped_pre_outbound` | 出站前因取消或预算终止 | 否 |

参数恢复重试与备用模型 fallback 各自成独立尝试，额外请求不藏在一次逻辑调用内。

### wire_model 归因来源与优先级

`wire_model` 是该尝试实际写到线路上的模型，按以下优先级归因：

1. **pre_api_call 观察（权威）**：per-call `logger_fn` 的 `pre_api_call` 事件携带 `additional_args.complete_input_dict`，其中 `model` 是 Router 选定部署后真正交给传输层的出站模型。传输层只读这个非空字符串标量（不读 prompt、正文、headers 或其他字段），事件触发即写入尝试——因此 429、读取超时、连接失败等无响应路径同样保留实际出站模型，在途快照与失败终态均可见；同一尝试内的再次出站以最后一次实际载荷为准。
2. **响应对象回退（best-effort）**：没有观察到有效非空出站 `model` 时（包括 observer 未触发，或 `pre_api_call` 已触发但载荷缺失/无有效 `model`），才沿用返回对象上的 `model` 字段；它可能是响应别名。
3. **未知（`None`）**：请求从未交给传输层（`queue_timeout`、`stopped_pre_outbound`、LiteLLM 本地校验失败）保持 `None`，不从配置 `target_model` 补成实际出站。

已观察到的请求模型不被后续响应对象、流 chunk 或失败终态覆盖成响应别名或 `None`；供应商在响应体中回显与请求不同的模型名按响应别名处理，不替换请求模型。配置的 `target_model` 与实际出站 `wire_model` 是两个字段，互不伪装。

## usage 存在性取证（确认零 vs 缺失）

LiteLLM 会把供应商未返回的 usage 合成为全零 `Usage` 对象，归一化后两者不可区分。本链路通过 **per-call `logger_fn`**（LiteLLM `post_call` 在响应转换前传入 `original_response`）在归一化前提取 usage 子对象（白名单过滤，不读 prompt、正文或 headers）：

- 供应商显式返回全零 → `usage_presence="provided"`、`usage_provided` 为全零 → **确认零消耗**（`completeness` 计入 known）。
- 供应商未返回 usage → `usage_presence="absent"` → **未知**，不冒充确认零。
- 观测未覆盖的路径（无 observer 证据但有响应对象）退回非零信号判定，保持旧行为。
- 闭包绑定 attempt 可变对象，跨 LiteLLM Router 的 `copy_context` 隔离稳定可见；不注册全局回调、不新增配置。

错误响应 usage 恢复：LiteLLM 对 4xx 抛异常时原始 httpx 响应体可能已被消费，但其公开的 `APIStatusError.body` 字典保留了解析后的供应商响应（含 usage）；`_extract_error_response_usage` 优先读 `.body` 的 usage 子对象（白名单过滤），4xx 响应本身即传输证据，usage 恢复并入账。

## 完整性语义

`summarize_attempts`（报告归集）：

- `known.input_tokens/output_tokens/total_tokens` 只合计供应商确实提供的字段（`normalize_litellm_usage` 的 `normalized_*`，`None` 即未知）。
- `unknown_fields.*` 计数未知字段；`completeness` 按逐字段判定：`complete`（三个核心字段全部已知）/ `partial`（至少一个字段确认且至少一个未知）/ `unknown`（无任何确认 usage）/ `no_outbound`。
- 部分字段 usage（如仅 `prompt_tokens`）是 `partial`；缺可选 reasoning/cache 明细不把已知输入/输出/总量降为未知；推理与缓存明细是输入/输出的组成部分，不重复叠加进总量。
- 旧数值接口兼容：`DualTrackResult.total_tokens` 与报告元数据 `total_tokens` 列保持"已知合计"语义；完整性通过追加的 `model_usage` 字段表达。

## 逐次审计与账本的关系（不双计）

- `llm_usage` 表的持久化点移入传输层尝试生命周期（`limited_completion` / `limited_acompletion` 内）：每次物理尝试（含参数恢复与备用模型的每次、解析失败/截断/传输异常但供应商已返回 usage 的失败）恰好写一行（`call_type=entry`），模型名沿用请求模型名与历史行一致，usage 先经既有归一化器（供应商缺失字段保持 NULL）。同一尝试的 `usage_record_id` 幂等一次；出站前终止或无 usage 不写行、不伪造行 ID，也不会把上一个成功尝试的 usage 重绑到新尝试。无 entry 的调用点（问股等）保持原状。
- `record_llm_usage` / `persist_llm_usage` 返回已提交行 ID；attempt 账本记录 `usage_record_id`，报告 sidecar 通过行 ID 与既有逐次表精确对账，不扩表、不回填、不双账本。写入失败或无 usage 不返回伪造 ID。
- 出站前终止（等待超时/取消/预算）不写审计行，不伪装真实调用。

## 流式与结构化解析的终态

- 流式响应到达时尝试保持 `in_flight`（未消费的流不是成功）；自然耗尽（StopIteration）在同一尝试上落 `ok` 并释放许可；流中尾部携带的 usage chunk 被采纳（确认零/部分字段同样区分）。
- 流中途读失败由 LiteLLM 包装为 `MidStreamFallbackError`（ServiceUnavailableError 族），按 `provider_error` 记终态，保留出站事实。对端提前关闭连接在 SSE 语义上等同提前 EOF，LiteLLM 视为正常耗尽（`ok`、chunk 较少）；该限制如实记录。调用方在自然完成前显式 `close()` 记 `truncated`（输出未完整消费，不是成功），取消与截断终态优先级不被覆盖，close 异常路径仍释放许可。
- 业务层结构化解析失败（研究员 JSON 提取、六维补充评分、经理终稿三波）归因到刚返回的同一尝试（`parse_error`），截断与取消终态优先级不被覆盖，已返回 usage 不丢。

## 报告产物、取消时序与读取归属

- 生成完成（含中断冻结）后，`{report_id}_dims.json` 追加 `model_usage`（`attempts` 为计数、`attempt_records` 为逐次明细，含 `origin_run_id`/`origin_report_id` 与 `usage_record_id`），SSE `done` 事件同步透传。
- 取消时序：渲染时刻的快照（`partial_ready` 前的 service 落盘）保留在途尝试的 `in_flight`/未知语义；**清理完成后**（`ResearchRun` finalized 且全部在途 future 返回，API `clean()` 内、终态事件发出与同股票去重释放之前）刷新：重算账本 → 重写 `_dims.json` 的 `model_usage` → 按 `report_id` 单列更新 DB `total_tokens` → 同步日缓存 payload → 更新同一 done 事件的 `model_usage`。正常完成路径该刷新幂等。
- 日缓存命中与历史读取引用原生成运行的 `model_usage`（`origin_run_id`），本次读取不新增模型出站、不复制用量记录；缓存命中路径（`cache_hit=True`）与 `origin_run_id` 非本次运行的终态不触发回写，调用者复用旧 run_id 也不会把旧报告用量重算归零。
- 历史报告缺少 `model_usage`（#55 之前生成）时按缺失读取，不回填虚构用量。

## 用量页口径

`TokenUsagePage` 展示的是**已确认的供应商用量**：仅 `llm_usage` 入表记录计入（供应商未返回 usage 的尝试不入表，空记录不代表零消耗或零调用）。行级追加 `completeness` 与 `unknown_fields`：供应商未提供的字段显示"未知"而非 0；`persist_llm_usage` 不再把 `normalized_*` 的 `None` 回填为 legacy 数值 0，部分字段未知在 API/页面可见。旧数值字段与旧客户端兼容。

## 基线与对照入口

`scripts/model_workflow_baseline.py`（阶段一由第二施工者收敛，见其交付说明）在本地回环供应商 + 冻结样本上重跑旧流程编排，输出版本、事实身份、规则输入、模型/推理强度、维度选择、缓存条件、逐次尝试、重复查询与模型占用；产物默认写 `.claude/reviews/stage1-baseline/`（非入库证据）。本地回环仅作计量 dry-run；真实质量与性能对照由维护者在同模型/推理强度/证据/缓存/并发条件下交错旧新各三次执行。

## 回滚

回滚本链路实现改动即可恢复旧行为：runner 恢复自身的逐次持久化点、报告不再携带 `model_usage`、页面恢复纯数值显示。历史报告、`llm_usage` 审计行与 `_dims.json` 均为追加数据，回滚不删除任何用量记录。
