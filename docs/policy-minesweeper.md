# 政策与公告双维度排雷（Policy & Filing Minesweeper）

> 本文档描述「政策与公告排雷」功能模块的设计、接口、评分模型、配置与边界。该模块在左侧菜单「深度投研」下方，提供 A 股政策与公告双维度排雷报告的生成、PDF 下载与历史管理。

## 1. 功能定位

- **入口**：左侧菜单「政策与公告排雷」（路由 `/policy-minesweeper`，紧随「深度投研」）。
- **形态**：表单输入 A 股代码/名称 + 时间窗口 → SSE 流式生成 → 报告展示 + PDF 下载 + 历史列表。
- **分析框架**：真·三 Agent 并行裁决——α-公司公告与经营事件扫描（微观 8 维）+ β-国家政策与产业互动分析（宏观 6 维 + 政策-业务映射 + DCF 三要素）→ Ω-综合裁决器（信号一致性 + 主导因子 + 时间窗权重）。
- **产出**：综合分（-100 强利空 ~ +100 强利好）、5 档等级 + 仓位指令、预期冲击区间（历史经验区间）、情景分析、证据链（来源+日期+公告原文地址）。
- **范围**：MVP 仅支持 A 股（沪深京：60/00/30/688/920/430/83 开头 6 位代码）。港股/美股后续扩展。

## 2. 架构

```
PolicyMinesweeperPage (表单+SSE+报告, 时间窗口选择器)
   │ fetch SSE
   ▼
api/v1/endpoints/policy_minesweeper.py (5 接口, /api/v1/policy-minesweeper/*)
   │
   ▼
src/services/policy_minesweeper_service.py (编排: 校验→生成→存盘→清理→best-effort 解析评分)
   │
   ├─ src/agent/policy_minesweeper_executor.py (α/β 并行 ThreadPoolExecutor → Ω 串行综合 + 降级)
   │     └─ run_agent_loop (复用, DI loop_runner 注入便于单测) × 3
   │     └─ 5 个问股工具 (精确筛选: 公告/新闻+情报+基本面+行情+板块) + score_policy_minesweeper + search_company_announcements
   │     └─ src/agent/tools/policy_minesweeper_tools.py (score_policy_minesweeper, Ω 调用)
   │           └─ src/services/policy_minesweeper_scorecard.py (确定性六维评分卡)
   │
   ├─ src/md2pdf.py (Markdown→PDF, WeasyPrint/Pango, 惰性生成)
   │
   └─ src/storage.py: policy_minesweeper_reports 表 (SQLite CRUD, 并发安全)
```

与问股/郑希/供应链对话框模式的差异：**表单一次性生成**（非多轮对话），历史通过 `policy_minesweeper_reports` 表持久化。与深度投研同构（表单流），但执行层为**三 Agent 并行**（非单 Agent 多步）。

## 3. API 接口

所有接口挂 `/api/v1/policy-minesweeper`，继承全局 AuthMiddleware。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/generate/stream` | SSE 流式生成。请求体 `{stock_code, stock_name?, horizon?}`；事件：`thinking`/`tool_start`/`tool_done`/`generating`/`done`/`error`/`heartbeat`（30s 心跳，含 `agent` 字段标记 α/β/Ω）|
| GET | `/reports?limit=50&offset=0` | 历史报告列表（分页，按时间倒序）|
| GET | `/reports/{report_id}` | 报告详情（含 Markdown 正文）|
| DELETE | `/reports/{report_id}` | 删除报告（元数据 + .md + .pdf）|
| GET | `/reports/{report_id}/pdf` | PDF 下载（惰性生成，`asyncio.to_thread`）|

**report_id 格式**：`{6位A股代码}_{YYYYMMDDHHmm}`，同分钟同股票冲突时追加 `_序号`；白名单 `^\d{6}_\d{12}(_\d+)?$` + 路径收敛双重防穿越。

## 4. 评分模型

六维方向评分（各 -100~+100，加权求维度综合分）：

| 维度 | 权重 | 含义 |
|------|------|------|
| event_importance | 0.20 | 事件重要性（利好+ / 利空-）|
| policy_exposure | 0.20 | 政策相关度/暴露度 |
| earnings_impact | 0.25 | 盈利影响 |
| valuation_impact | 0.15 | 估值影响 |
| price_sensitivity | 0.10 | 股价敏感度（市值/流动性/Beta）|
| time_urgency | 0.10 | 时间紧迫度 |

**时间窗口动态权重**（α 公司层面 : β 政策层面）：short `0.7:0.3` / medium `0.5:0.5` / long `0.3:0.7`。

**最终分** = `0.6 × 维度综合分 + 0.4 × 时间窗加权分`（clamp 到 -100~+100）。

**5 档等级 + 仓位指令**：

| 区间 | 等级 | 仓位指令 |
|------|------|---------|
| ≥ +60 | 🟢 强利好 | 加仓 |
| +20 ~ +59 | 🟢/🟡 偏利好 | 增持 |
| -19 ~ +19 | 🟡 中性 | 持有/观望 |
| -59 ~ -20 | 🟠 中等利空 | 减持 |
| ≤ -60 | 🔴 强利空 | 清仓/回避 |

> 仓位指令为模型输出的一部分，**衡量政策/公告对股价的利好利空冲击，非精确预测**；预期冲击区间为历史经验区间。详见报告末尾免责声明。

## 5. 配置

| 环境变量 | 默认 | 说明 |
|---------|------|------|
| `POLICY_MINESWEEPER_MAX_REPORTS` | `200` | 保留报告数量上限（超出删最旧，含 .md/.pdf 文件；`0` = 不清理）|
| `SERPAPI_API_KEYS` | _(空)_ | 公告/新闻搜索引擎 key（逗号分隔多个）。配置后 `search_company_announcements`/`search_stock_news` 可用，否则 α 公告证据只能标注「待核验」 |

**系统依赖**：PDF 生成复用 `src/md2pdf.py`（WeasyPrint / Pango+Cairo 渲染，正确处理 CJK、emoji、表格、列表项目符号）。macOS 自动探测 Homebrew lib 路径；Docker/CI 需装 `pango`/`cairo`/`fonts-noto-cjk`（已在后端镜像与 CI 中配置）。渲染失败时优雅降级（返回 404，不影响报告生成主流程）。

**nginx（生产）**：SSE 长连接需 `proxy_read_timeout ≥ 960s` + `proxy_buffering off`（端点 `STREAM_QUEUE_TIMEOUT_S=960`，略低于 nginx 超时）。

## 6. 存储与清理

- **元数据**：SQLite 表 `policy_minesweeper_reports`（id/stock_code/stock_name/created_at/md_path/pdf_path/status/horizon/alpha_ok/beta_ok/omega_ok/composite_score/verdict/confidence/total_steps/total_tokens/provider）。并发安全（`_run_write_transaction` 带锁重试）。
- **正文/PDF**：`reports/policy_minesweeper/{report_id}.md` 与 `.pdf`（Docker `/app/reports` volume 持久化）。
- **清理**：生成后若总数 > `POLICY_MINESWEEPER_MAX_REPORTS`，删最旧（事务内删元数据 + 事务外删文件）。

## 7. 边界与降级

- **三 Agent 降级**：α/β 任一失败 → Ω 基于可用腿 + 已知信息裁决（status=`partial`，显式标注不可用腿）；Ω 失败但 α/β 可用 → 降级输出公司/政策原始分析（无综合评分/仓位指令）；三者全失败 → status=`failed`，返回空正文 + 错误信息。
- **公司公告检索**：α 优先调用 `search_company_announcements`（复用共享 SearchService 的 SerpAPI provider，走原生 `.search()` 保留一手公告源），命中巨潮资讯网/新浪证券 `vCB_AllBulletin`/东方财富公告页/公司官网等，每条带**公告原文地址 `url`** + `is_official`（一手/二手标注）。`SERPAPI_API_KEYS` 未配或检索失败 → 返回 error，α 据此标注「待核验」而非编造。
- **数据缺失**：无专门政策数据源 → LLM 基于公开信息推断，**必须标注「待核验」**；预期冲击区间标注「历史经验区间，非精确预测」。证据必须含来源+日期；**公司公告类证据必须附公告原文地址 `url`**（取自检索工具结果，报告内以 `[原文](url)` 渲染，取不到则标注「待核验」）。
- **超时**：executor α/β `max_steps=10`/`wall=300s`、Ω `max_steps=6`/`wall=240s`（并行 α/β + 串行 Ω，总时长约 9 分钟，低于 SSE 960s 超时）；前端 90s watchdog 断线检测。
- **认证**：单管理员模型（历史报告全局可见，无用户隔离）。

## 8. 排障

| 现象 | 排查 |
|------|------|
| 生成超时 | 三 Agent 总时长约 9 分钟；检查 LLM 配额/网络；nginx `proxy_read_timeout` 是否 ≥ 960s |
| 报告标注「不完整」 | 某角色（α/β/Ω）失败降级，可查看报告内 `α/β/Ω` 状态标记定位降级来源，或重新生成 |
| PDF 下载 404 | 检查后端日志 `[md2pdf]` warning；确认报告正文（.md）非空；Docker 环境确认 pango/cairo 已装 |
| 综合分为空 | 评分卡 banner 解析失败（best-effort），正文 Markdown 仍含完整评分；可重新生成 |
| 非 A 股被拒 | MVP 仅支持 A 股（6 位数字代码）；港股/美股待扩展 |
