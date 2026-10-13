# A股深度投研报告（Deep Research）

> 本文档描述「深度投研」功能模块的设计、接口、配置与边界。该模块在左侧菜单「首页」下方，提供机构级 A 股深度投研报告的生成、PDF 下载与历史管理。

## 1. 功能定位

- **入口**：左侧菜单「深度投研」（路由 `/deep-research`，菜单第二位）。
- **形态**：表单输入 A 股代码/名称 → SSE 流式生成 → 报告展示 + PDF 下载 + 历史列表。
- **分析框架**：五层穿透（宏观定方向 → 产业定赛道 → 财务定质地 → 估值定价格 → 博弈定节奏），每层强制注入「政策敏感度」与「筹码结构」双因子。
- **范围**：MVP 仅支持 A 股（沪深京：60/00/30/688/920/430/83 开头 6 位代码）。港股/美股后续扩展。

## 2. 架构

```
DeepResearchPage (表单+SSE+报告)
   │ fetch SSE
   ▼
api/v1/endpoints/deep_research.py (5 接口, /api/v1/deep-research/*)
   │
   ▼
src/services/deep_research_service.py (编排: 校验→生成→存盘→清理)
   │
   ├─ src/agent/deep_research_executor.py (ReAct + 五层穿透prompt + 降级 + 质量重生成)
   │     └─ run_agent_loop (复用) + 10 个问股工具 (精确筛选)
   │     └─ src/agent/deep_research_validator.py (三层防线 L2 检测)
   │
   ├─ src/md2pdf.py (Markdown→HTML→PDF, xhtml2pdf+reportlab CID CJK, 惰性+Semaphore限流)
   │
   └─ src/storage.py: deep_research_reports 表 (SQLite CRUD, 并发安全)
```

与问股/郑希/供应链对话框模式的差异：**表单一次性生成**（非多轮对话），历史通过 `deep_research_reports` 表持久化（非 conversation 表）。

## 3. API 接口

所有接口挂 `/api/v1/deep-research`，继承全局 AuthMiddleware。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/generate/stream` | SSE 流式生成。事件：`thinking`/`tool_start`/`tool_done`/`generating`/`done`/`error`/`heartbeat`（30s 心跳）|
| GET | `/reports?limit=50&offset=0` | 历史报告列表（分页，按时间倒序）|
| GET | `/reports/{report_id}` | 报告详情（含 Markdown 正文）|
| DELETE | `/reports/{report_id}` | 删除报告（元数据 + .md + .pdf）|
| GET | `/reports/{report_id}/pdf` | PDF 下载（惰性生成，`asyncio.to_thread` + Semaphore 限流）|

**report_id 格式**：`{6位A股代码}_{YYYYMMDDHHmm}`，白名单 `^\d{6}_\d{12}$`（防路径穿越）。

## 4. 配置

| 环境变量 | 默认 | 说明 |
|---------|------|------|
| `DEEP_RESEARCH_MAX_REPORTS` | `200` | 保留报告数量上限（超出删最旧，含 .md/.pdf 文件；`0` = 不清理）|

**系统依赖**：PDF 生成采用**纯 Python 方案**（`xhtml2pdf` + `reportlab` 内置 CID CJK 字体 `STSong-Light`），`pip install -r requirements.txt` 后即可工作，**无需安装任何系统二进制或字体文件**，macOS / Linux / Docker 全平台一致。渲染失败时优雅降级（返回 404，不影响报告生成）。

> 历史说明：早期版本依赖 `wkhtmltopdf`（经 `imgkit`）。但 `imgkit` 调用的是 `wkhtmltoimage`（主输出图片，不支持 PDF 输出），且 `wkhtmltopdf` 已于 2023 年停止维护、Homebrew 6.0+ 移除了对应 formula，导致 macOS 本地下载 404。已切换为纯 Python 方案。

**nginx（生产）**：SSE 长连接需 `proxy_read_timeout 1200s` + `proxy_buffering off`（与供应链 SSE 一致）。

## 5. 质量保障（三层防线）

深度研报的质量是核心，采用三层防线防止 LLM 跳层/敷衍：

1. **L1 预防**：精确工具集（10 个，去掉 backtest 噪音）+ system prompt 检查点（每层必须调用特定工具）。
2. **L2 检测**：`DeepResearchValidator` 校验五层覆盖（关键词 + 工具调用）、结论前置（≥7）、三情景概率和（≈100%）。
3. **L3 兜底**：校验失败 → 追加提示重生成 1 轮；再失败 → 输出报告 + 顶部标注「⚠️X 层不完整」。步数耗尽 → 从已收集数据生成部分报告（不返回空）。

详见 `data/deep_research/system_prompt.md`（五层穿透框架，可迭代不改代码）。

## 6. 存储与清理

- **元数据**：SQLite 表 `deep_research_reports`（id/stock_code/stock_name/created_at/md_path/pdf_path/status/quality_score/missing_layers/...）。并发安全（`_run_write_transaction` 带锁重试）。
- **正文/PDF**：`reports/deep_research/{report_id}.md` 与 `.pdf`（Docker `/app/reports` volume 持久化）。
- **清理**：生成后若总数 > `DEEP_RESEARCH_MAX_REPORTS`，删最旧（事务内删元数据 + 事务外删文件）。

## 7. 边界与降级

- **数据缺失**：政策评分/行业高频价/龙虎榜等无专门数据源 → LLM 基于公开信息推断，**必须标注「数据缺失/基于推断」**，禁止编造。
- **单数据源失败**：复用 `DataFetcherManager` 多源 fallback，不拖垮报告。
- **超时**：executor `max_steps=30` / `wall_clock=1200s`；SSE 30s 心跳；前端 90s watchdog 断线检测。
- **认证**：单管理员模型（历史报告全局可见，无用户隔离）。

## 8. 排障

| 现象 | 排查 |
|------|------|
| PDF 下载 404 | 检查后端日志 `[md2pdf]` 相关 warning（渲染失败原因）；确认报告正文（.md）非空；纯 Python 渲染无需系统依赖，404 多为正文空或 xhtml2pdf 缺失 |
| 盘中报告「当前价」=昨收 | `analyze_trend` 已在盘中自动合并今日实时行情 bar（日志 `盘中合并今日实时 bar`）；若仍异常，检查 `get_realtime_quote` 数据源是否可用、当日是否交易日 |
| 生成超时 | 检查 LLM 配额/网络；nginx `proxy_read_timeout` 是否 ≥ 1200s |
| 报告标注「不完整」 | 五层穿透某层工具未调用或内容缺失，可重新生成 |
| 非 A 股被拒 | MVP 仅支持 A 股（6 位数字代码）；港股/美股待扩展 |

## 缺失估值与刷新

情景概率不等于目标价证据。当前情景载荷不包含目标价来源及计算依据，因此保留定性情景，取消现价固定倍数和无来源模型目标价生成的 EV、价值区间与时间目标；仅有 PE/PB 数字也不构成完整估值。相关情景、结论和信号明确降级为观察，独立技术点位仍按已有 ATR/支撑规则提供，不增加新的估值模型。

强制刷新传递既有财务刷新上下文到阶段 0，按当前请求/标的隔离并在退出时复原。依赖本次行情/财务的维度重新计算，较慢变化的资料仍可按 TTL 复用，保持原始 as_of。partial 报告不写入成功日缓存；A 股缺少 PE/PB 或核心财务指标的 manager 结果及阶段 0 快照不读写缓存，下一次可重新尝试取数。历史报告不被静默重算或覆盖。
