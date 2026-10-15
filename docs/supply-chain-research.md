# 供应链分析（Serenity 方法）

「供应链分析」入口在侧栏 `/supply-chain`，用 **Serenity「供应链卡点猎手」9 步深度调研方法**做投研：主题扫描、单公司挑战、候选对比、瓶颈打分。所有结论**标注证据强度、不编造数据、不给买卖指令**。当前主路径为**表单式报告流**（输入分析主题 → SSE 生成 → 报告展示 → 历史列表 → PDF 下载，可选附「供应链线索」一次性调查目标）；旧聊天式端点保留可用。

## 功能定位

用户提问 → 系统按 9 步 pipeline 深度调研 → 返回白话排序 + 卡点层 + 证据 + 证伪条件。

方法论来源：开源 skill [serenity-skill](https://github.com/gyc567/serenity-skill)（MIT，复刻公开投研方法），落盘在 `data/supply_chain_skill/`。

## 9 步 pipeline（核心方法论）

```
市场故事 → 系统变化 → 需要的零部件 → 产业链层级 → 稀缺约束（卡点）
       → 上市公司 → 证据 → 市场可能忽略了什么 → 什么会证伪
```

- 先排**价值链层级**（8 层 checklist：终端/集成/模块/芯片/工艺/设备/材料/基础设施），再排公司
- **稀缺层识别**：客户无它无法扩产、供应商少、验证周期长、扩产需特种设备/许可
- 候选池 ≥20 家（含"需降级的热门股"），证据 ≥25 源（条件性，工具/时间不允许则声明"初步筛选"）
- 证据分级：`primary`（交易所文件/年报/电话会/官方订单）/ `media` / `analysis` / `social` / `rumor`，无源标"待核验"

## 能力

| 能力 | 触发问法 | 工具 |
|---|---|---|
| 主题扫描 | "分析 A 股 AI 半导体供应链" | 复用问股工具（行情/新闻/基本面）+ 9 步 pipeline |
| 单公司挑战 | "挑战中际旭创是不是 CPO 核心" | 复用问股工具查数据 + 卡点核查 |
| 候选对比 | "比较几家光通信设备商" | 复用问股工具 + 横向排序 |
| 瓶颈打分 | "给 XX 打供应链瓶颈分" | `score_supply_chain_bottleneck`（8 因子 + 8 惩罚，满分 100） |
| 半导体/AI 一级研究 | "HBM3E 供给""CoWoS 产能""Blackwell 供应链" | `search_semianalysis`（站点限定检索 semianalysis.com，返回带原文 url 的分析文章） |
| 线索炒作信号 | 用户提供线索时 | `search_clue_hype`（跨新浪财经/雪球/同花顺/巨潮公司公告/全网检索线索，任一媒体提及=题材炒作加分项，返回提及源+信号强度无/弱/中/强） |
| 双源校验 | A 股候选标的进最终表前 / 涉及 A 股公司板块的线索 | `verify_supply_chain_evidence`（东方财富 + 同花顺结构化校验公司/板块归属，返回 status+confidence+两源证据+成分股重合度） |
| 研究/学习对话 | "带我学这套方法" | serenity-dialogue-protocol（每轮一问） |

## 合规红线（必须遵守）

1. **禁止直接买卖指令**。强制措辞："我会按优先研究价值排序。买卖动作由你自己决定。"
2. **禁止炒作小票/社交驱动标的**；遇到先拉回证据、流动性、稀释、估值。
3. **禁止编造**价格/文件/客户/订单/合同/市值。数字必须有源，无源标"待核验"。
4. 输出先结论（纯文本，非券商报告腔）→ 层级排序 → 紧凑表格（标的|卡住的环节|为什么排这里|关键证据|主要风险）→ 证伪条件 + 下一步。

## 技术架构

- **executor**：`src/agent/supply_chain_executor.py` 的 `SupplyChainExecutor`，复用 `run_agent_loop` / `LLMToolAdapter` / `conversation_manager` / SSE 包装。
- **工具集**：**复用问股 `get_tool_registry()` 的 18 个工具**（行情/基本面/新闻/技术）+ 3 个供应链专属工具（`score_supply_chain_bottleneck` 瓶颈打分 + `search_semianalysis` 半导体/AI 主题检索 semianalysis.com + `search_clue_hype` 线索多源炒作检索），装入独立 ToolRegistry 实例（复制问股工具，不污染全局单例；专属工具仅注册给供应链 executor，问股/郑希/排雷零影响）。
- **线索炒作检索**：`search_clue_hype` 在用户提供「供应链线索」时，复用共享 `SearchService` provider（配置 `SERPAPI_API_KEYS` 等后可用），跨 5 个固定源——新浪财经（`site:finance.sina.com.cn`）/雪球（`site:xueqiu.com`）/同花顺（`site:10jqka.com.cn`）/巨潮公司公告（`site:cninfo.com.cn`）/全网 Google（不限）——用 `site:` 限定逐源检索线索（半年窗 180 天）。**单源异常/失败不拖垮整体**（逐源 try/except）。任一源提及即题材炒作加分项；按提及源数给信号强度（0=无/1-2=弱/3-4=中/≥5=强）。prompt 已加 standing 规则：线索非空必调，报告新增「题材炒作信号」小节列出提及媒体+原文链接，并把提及广度纳入 `hype_risk`（炒作风险）评分。
- **SemiAnalysis 检索**：`search_semianalysis` 复用共享 `SearchService` 的 provider（配置 `SERPAPI_API_KEYS` 等后可用），query 前缀 `site:semianalysis.com` 站点限定（零改共享 `search_service`），时间窗 365 天（长青研究），返回带 `url` 的结果；无可用 provider 时 fail-open 返回 error，agent 标注「待核验」。prompt 已加 standing 规则：半导体/AI 主题（芯片/HBM/先进封装/光刻/GPU/数据中心 AI 硬件/硅光子 CPO 等）必调，证据强度按 `analysis`（一手调研升 `primary`），付费墙内容只引可见摘要。
- **打分**：`src/services/supply_chain/scorecard.py` 通过 importlib 加载 `data/supply_chain_skill/scripts/serenity_scorecard.py`（纯函数，无需 subprocess）。
- **system prompt**：运行时组装 SKILL.md + 核心 5 references（deep-research-workflow / evidence-ladder / market-source-playbook / serenity-dialogue-protocol / output-style-and-language）+ 合规红线 + **工具结果摘要约束**。
- **API**：`api/v1/endpoints/supply_chain.py`（旧聊天，`POST /api/v1/supply-chain/chat/stream` + 会话 CRUD，保留）+ `api/v1/endpoints/supply_chain_reports.py`（新报告流，`POST /generate/stream` + `/reports` CRUD + `/reports/{id}/pdf`，与旧 chat 同前缀路径不冲突）。
- **会话隔离**：旧聊天 `supply_chain:` 前缀（复用 `session_prefix` 过滤，与问股/郑希 3 路不串台）；新报告流走独立表 `supply_chain_reports`，内部 session `supply_chain_report:{report_id}`，与旧聊天互不可见。
- **报告 service**：`src/services/supply_chain_report_service.py`，编排生成→落盘→清理→PDF 惰性生成（复用 `src/md2pdf.py`），不新增配置项（保留数量用 service 常量 200）。
- **前端**：`pages/SupplyChainReportPage.tsx`（新主页面，表单式报告流）+ `useSupplyChainReport` hook + `api/supplyChainReports.ts`；旧 `pages/SupplyChainChatPage.tsx` / store / api 保留可回滚。

## 表单式报告流（报告端点 / 线索 / PDF）

主路径从聊天式升级为表单式报告流：表单输入分析主题（+ 可选供应链线索）→ SSE 生成 → 报告展示 → 历史报告 → PDF 下载。

**表单字段**（仅两个核心字段，第一阶段不强约束 `mode`）：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| 分析主题 | 是 | 如「A 股 AI 半导体供应链」「光模块产业链瓶颈」「中际旭创是不是 CPO 核心卡点」；前端提供快捷模板按钮填充 |
| 增加供应链线索 | 否 | 本轮一次性调查目标（客户/供应商/订单/技术路线/产能/政策关键词）；发送后清空、不回填历史线索、不污染下一轮 |

**报告端点**（`/api/v1/supply-chain/...`，与旧 chat 同前缀、路径不冲突）：

- `POST /generate/stream` —— SSE 流式生成（事件 `thinking` / `tool_start` / `tool_done` / `generating` / `heartbeat` / `done` / `error`，30s 心跳）。`done` 携带 `report_id` / `markdown` / `status` / `total_steps` / `total_tokens` / `provider`。
- `GET /reports` / `GET /reports/{id}` / `DELETE /reports/{id}` —— 历史报告列表（按时间倒序）/ 详情（含 Markdown 正文）/ 删除（元数据 + `.md` + `.pdf`）。
- `GET /reports/{id}/pdf` —— PDF 惰性下载（首次请求触发渲染，后续直接发文件）。

**线索规则与注入**：线索非空时，service 把用户输入包装成高权重调查指令拼进本轮 user message（主动搜索公告/财报/新闻/上下游、关键说法至少两类来源交叉验证、同时找支持-冲突-证伪、把验证结果写入「线索验证」小节、说明对层级排序/候选标的/瓶颈分/风险的影响）。线索为空时只发分析主题，行为与普通供应链报告一致。system prompt 新增「线索核验规则」standing 段：线索是调查目标非事实、优先级低于工具证据、不能因用户给线索就强行确认、订单/客户/供应商/产能/市占率/政策须标来源强度；报告须含「线索验证」小节（验证状态限 `已确认 / 部分确认 / 未找到可靠证据 / 存在冲突 / 已证伪`，无线索可省略）。

**`report_id` 与安全**：格式 `sc_{YYYYMMDDHHmm}_{seq}`（纯 ASCII，topic 不进路径），白名单 `^sc_\d{12}(_\d+)?$` + `_resolve_safe_path`（resolve 后须在 `reports/supply_chain/` 内）双重防路径穿越，越界按 404 不泄露真实路径。请求 schema 用 Pydantic v2 `ConfigDict(strict/frozen)` + `Field(min_length/max_length)`（空/超长主题解析期 422）。status 从 `AgentResult` 派生：`success` / `partial`（失败但有正文，仍落盘可查）/ `failed`。

**回滚**：前端 `/supply-chain` 路由 element 改回 `SupplyChainChatPage`；后端移除 `supply_chain_reports` 端点文件与 router 一行即可恢复原聊天功能；新表与旧会话隔离，删 `reports/supply_chain/` 清理新产物。

## 长任务特性（与问股/郑希的关键差异）

供应链是**深度调研**，不是快速问答：

- `max_steps=40`（问股/郑希 10），9 步 pipeline 多次工具调用
- `wall_clock=1200s`（20 分钟上限），单次问答通常 5–15 分钟
- SSE event 间隔 timeout **1200s**（问股/郑希 300s）
- 前端空态/副标题提示"深度调研模式，预计 5–10 分钟"
- SSE 进度事件（thinking/tool_start/tool_done）实时显示调研过程，是天然进度条
- `max_steps` 硬编码在 `build_supply_chain_executor`（方案 A），**不碰 `config.agent_max_steps`**，问股/郑希零影响

## 双源校验（公司 / 板块归属）

为防止 LLM 把单源搜索结果写成已确认事实，供应链报告中的「公司 / 板块归属」类事实必须经过东方财富 + 同花顺双源结构化校验（A 股限定）。

- **工具**：`verify_supply_chain_evidence(stock_code, stock_name, claim, board_hint?, topic?)`。
- **判定层**：`data_provider/supply_chain/cross_source.py` —— 纯逻辑判定（代码归一 / 名称标准化 / 板块匹配 / 成分股 Jaccard 重合度 / 决策表）+ `SupplyChainSourceProbe` Protocol（N 源可插拔）+ fail-open IO 探针。镜像 `data_provider/cross_source_validator.py` 设计，判定与取数解耦。
- **状态枚举**：`confirmed`（双源命中）/ `partial`（仅一源支持，另一源不可用或未定位到板块）/ `conflict`（两源均定位到相关板块但归属相反）/ `unverified`（两源不可用或均无命中）/ `not_applicable`（非 A 股）。对应置信度 high / medium / low。
- **降级语义**：单源异常 / 不可用绝不拖垮另一源（逐源 try/except）；公开源失败 = "未核验"而非"否定"。校验失败不阻断报告生成，报告按状态标注「待核验 / 单源支持 / 双源冲突」。
- **范围限定**：只支持 A 股（6 位代码）。港股、美股、无法归一到 6 位代码的标的直接 `not_applicable`。
- **同花顺来源**：akshare `stock_board_concept_name_ths()`（概念列表，列 `name`/`code`）+ 同花顺概念详情页 `q.10jqka.com.cn/gn/detail/code/{code}/order/desc/page/{N}/`（GBK 正则解析成分股，分页至空页、封顶 10 页）。源标记 `akshare_ths`（akshare 1.18 已无 `stock_board_concept_cons_ths`，故成分股走详情页抓取；`http_get` 可注入便于离线单测）。iFinD MCP 当前仅覆盖估值/财务数值锚点，不支持板块成分股结构化查询；如需 iFinD 优先，可新增实现 `SupplyChainSourceProbe` 的探针注入 validator，无需改判定层。
- **报告契约**：A 股标的最终候选表必须含「东财校验 / 同花顺校验 / 双源状态」三列；未得 `confirmed` 不得写成已确认事实。`search_clue_hype` / `search_semianalysis` 只证「被提及」，与「板块归属支持」不可混用。

## 成本预期

单次深度调研 = 40 步 LLM 往返 + 几十次工具调用 ≈ **50–200K tokens**（约为问股/郑希的 10–20 倍）。成本计入系统 `llm_usage`（`/api/v1/usage` 可查）。当前版本无配额限速，后续可加每会话深度调研次数限制。

## 数据局限（诚实处理）

Serenity 要求 ≥25 源含公告/招投标/环评/专利等深度源；项目 `data_provider` 只有行情/新闻/基本面。pipeline 会完整跑，但深度源靠：
- 复用问股工具（`search_comprehensive_intel` 间接搜公告/财报线索）
- LLM 自身知识
- evidence-ladder 的"待核验"标注

二期接公告/招投标专用源可提升证据精度。

## 部署提示（nginx / 反向代理超时分层）

**分层原则：外层（nginx/proxy）超时必须 ≥ 内层（app）超时**，否则代理会在 app 仍在合法处理时先掐断连接。app 侧上限 = SSE event 间隔 timeout `1200s` = executor `wall_clock=1200s`，因此 nginx `proxy_read_timeout` 必须 **≥ 1200s**（建议留余量 `1300s`）。

项目自带链路（FastAPI + uvicorn）**不会截断** 1200s 长任务。但若部署在 nginx/反向代理后，默认 `proxy_read_timeout 60s` **会截断** SSE 流，必须按上面的分层原则配置：

```nginx
location /api/ {
    proxy_read_timeout 1200s;     # ≥ app SSE/executor 上限 1200s（建议 1300s 留余量）
    proxy_send_timeout 1200s;
    proxy_buffering off;          # 或依赖响应头 X-Accel-Buffering: no（后端已设）
}
```

> `proxy_read_timeout` 在 nginx **每收到一个 SSE event 就重置**，所以只要调研过程中事件间隔远小于此值（通常每几秒一条 thinking/tool 事件），连接就不会被掐；该上限只在"长时间无任何事件"的极端静默（如单次 LLM 调用 >1200s）时触发。

**客户端断连的孤儿线程成本（已知边界）**：SSE 端点把 executor 跑在线程池里，Python 线程无法强制取消。若客户端中途断开或代理提前掐断，后端 executor 仍会跑到自身 `wall_clock=1200s` 上限才结束——最坏情况会多消耗约一次深度调研的 token（50–200K）。当前不做协同取消（避免改动共享 `run_agent_loop`，保持问股/郑希零影响）；二期可给 `run_agent_loop` 加可选取消信号。

## 验证

```bash
python scripts/check_supply_chain_data.py        # 数据完整性
python -m pytest tests/test_supply_chain_services.py tests/test_supply_chain_semianalysis_tool.py tests/test_supply_chain_clue_hype_tool.py   # scorecard + 工具 + SemiAnalysis 检索工具
python -m pytest tests/test_supply_chain_report_storage.py tests/test_supply_chain_report_service.py tests/test_supply_chain_report_api.py   # 报告流 storage/service/api
cd apps/dsa-web && npm run lint && npm run build   # 前端（表单式报告页）
```

## 配置

复用现有 Agent 配置（`AGENT_MODE` / LLM 渠道），无新增环境变量。Agent 未启用时供应链端点返回 400。`max_steps=40` / `wall_clock=1200s` 在 `build_supply_chain_executor` 硬编码（后续可抽到 config）。

## 后续版本（未实现）

- **token budget 熔断**：当前靠 max_steps + wall_clock + prompt 摘要约束；二期给 `run_agent_loop` 加可选 `token_budget` 参数（默认 None 不影响问股/郑希）。
- **公告/招投标/环评专用工具**：提升证据精度（当前靠搜索间接）。
- **每会话深度调研配额**：控制成本。
- **深度调研长任务 UI**：更明显的进度/取消/后台化。
