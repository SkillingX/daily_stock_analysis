# x.com（Twitter/X）海外媒体消息源接入方案 · v3（查证定稿）

> **状态更新（2026-10，issue #46）**：x.com 消息源与社区评价能力已退役，`XSearchProvider` 已删除；本文保留为历史方案说明。


- 日期：2026-06-30
- 状态：**已实现**（Phase 0–6 完成；查证复用 adanos，零新依赖/零新 env；39 用例 + 2 opt-in 真 API 集成测试全绿）
- 相关代码：`src/search_service.py`、`src/services/social_sentiment_service.py`、`src/core/pipeline.py`、`src/config.py`
- 关联约定：`AGENTS.md`（目录边界 / 配置开关 / 单一源失败不拖垮主流程）

---

## 1. 决策摘要

| 维度 | 决策 |
|---|---|
| 获取方式 | **复用现有 adanos**（`api.adanos.org`，已集成）——查证确认其支持 per-ticker X 数据，**零新外部依赖** |
| 数据端点 | `GET /x/stocks/v1/stock/{ticker}` ——一次调用同时返回**聚合舆情 + top_tweets** |
| 数据形态 | 逐条推文（`top_tweets` → NewsIntel 新闻条目）+ 聚合舆情（buzz/sentiment 分数） |
| 集成路径 | **A**：新增 `XSearchProvider` → 经 SearchService 自动流入 `search_stock_news`/`comprehensive_intel` → NewsIntel |
| 覆盖市场 | US 全覆盖；HK 经 ADR 映射；A 股大概率 `found:false` → fail-open（x.com 客观现实） |
| 评分接入 | 仅 LLM 文本上下文，**不改** `score_sentiment()` |

## 2. 查证结论（Phase 0 解除阻塞的依据）

直接探测 adanos 公开 OpenAPI（`/openapi.json`、`/x/stocks/v1/`）确认：

**`/x/stocks/v1/stock/{ticker}`**（Stock Sentiment）返回字段：
```
聚合舆情：buzz_score, sentiment_score, bullish_pct, bearish_pct,
         positive/negative/neutral_count, mentions, unique_tweets, trend, daily_trend
逐条推文：top_tweets, top_authors        ← 标准账号即可
元数据：  company_name, ticker, found, period_days
```
- `found:false` 表示该 ticker 在 X universe 无覆盖（A 股常见）→ provider 返回空、fail-open。
- **raw 全量推文** `/stock/{ticker}/mentions` 需 **Professional 账号**；标准账号仅 `top_tweets`（代表性，数量有限）。
- `/search?q=` 是**股票名→ticker 解析器**（非推文搜索），可用于 HK→ADR 映射辅助。
- 现有 `SocialSentimentService` 只用了 `/trending`（全市场热度，US-only 注入）；本方案新增的是**逐只股票**维度，职责互补、不重复。

**结论**：无需选型/付费新聚合服务，复用现有 `SOCIAL_SENTIMENT_API_KEY` + `SOCIAL_SENTIMENT_API_URL` 即可。

## 3. 审计与查证演进（v1 → v2 → v3）

| 版本 | 关键变化 |
|---|---|
| v1 | 两组件 + 新模块文件 + 改 pipeline + 放 US 门禁 + 5 个新 env + 选新聚合 |
| v2 | 收敛为一个 `XSearchProvider`；不动 pipeline/门禁；消除重复 X 块；但仍假设需**新**聚合 + 新 env |
| **v3** | **查证**：adanos 已支持 per-ticker 情绪+top_tweets → **零新依赖**、**零新 env**（复用 `SOCIAL_SENTIMENT_API_KEY/URL`）、Phase 0 解除阻塞 |

## 4. 架构（Path A，adanos 支撑）

```
SOCIAL_SENTIMENT_API_KEY/URL（现有）
        │
        ▼
XSearchProvider（新增, src/search_service.py 内）
   ├─ 按市场解析 ticker（US: code 即 ticker；HK: 映射 ADR；A: found:false→空）
   ├─ GET /x/stocks/v1/stock/{ticker}  (X-API-Key, 复用 tenacity 重试 + 线程安全缓存)
   └─ top_tweets → SearchResult；buzz/sentiment 可作摘要行
        │ 注册进 SearchService._providers
        ▼
search_stock_news / search_comprehensive_intel（自动遍历 + fallback + 排序 + 去重）
        ▼
NewsIntel（provider="X", source=@handle/x.com）→ intel_report → news_context
        ▼
主流程 / deep-research / 问股 / 郑希 全部自动可见

（保持不动）SocialSentimentService.fetch_x_trending (US 全市场热度) → news_context
```

## 5. 组件设计：`XSearchProvider`（`src/search_service.py`，模板 `AnspireSearchProvider` @ L1073）

- `__init__(self, api_keys, api_url)` → `super().__init__(api_keys, "X")`；`api_keys` 复用 `[social_sentiment_api_key]`，`api_url` = `social_sentiment_api_url`。
- **ticker 解析**（市场自适应）：US→`code`；HK→ADR 映射表（腾讯→TCEHY 等，可配/可查 `/search`）；A→大概率无覆盖。
- `_do_search(query, api_key, max_results, days) -> SearchResponse`：
  - `GET {api_url}/x/stocks/v1/stock/{ticker}`，header `X-API-Key`。
  - **防御性解析**：`found:false`/异常/空 → 空 `SearchResponse`（fail-open，绝不抛）。
  - 映射 `top_tweets` → `SearchResult(title=tweet_text, snippet=..., url=tweet_url, source=@author, published_date)`；可选把 `buzz_score/sentiment_score` 作首条摘要行。
  - 新鲜度过滤（复用 `news_max_age_days`）。
- **ticker 传入**：搜索流程的 `query` 是字符串，而本 provider 需按 ticker 查。两种实现（Phase 2/3 定）：
  - (a) 在 `search_stock_news` 增 `isinstance(provider, XSearchProvider)` 分支传 `stock_code`（类比 Tavily `topic` / Brave `search_lang` 的特殊参数处理）；
  - (b) provider 内从 query 解析 ticker（US 可行，HK/A 不可靠）。
  - 推荐 (a)，更稳。

## 6. 市场覆盖（现实约束）

| 市场 | 覆盖 | 处理 |
|---|---|---|
| US | 全（`AAPL`/`TSLA`） | 直连 |
| HK | 部分（需 ADR 映射） | 映射表 + `/search` 兜底解析 |
| A | 极少（`found:false`） | fail-open 静默跳过 |

「全市场」实际为「美股为主、港股靠 ADR、A 股基本无信号」——x.com 客观现实，非设计缺陷。

## 7. 配置（**零新 env**，复用现有）

- 复用 `SOCIAL_SENTIMENT_API_KEY` + `SOCIAL_SENTIMENT_API_URL`（已存在于 `config.py`、`.env.example`）。
- 不新增 `X_API_KEYS` 等（v2 的设想被查证推翻）。
- 启用：`SOCIAL_SENTIMENT_API_KEY` 存在即启用 X provider（与 SocialSentimentService 共享同一 adanos 账号/配额，per-stock 缓存 + 熔断防爆）。
- 可选：若需独立开关 X-in-news 与 US social 注入，未来加 `X_SEARCH_ENABLED`（当前按 §7 不加，避免开关膨胀）。

## 8. 改动清单（v3 精简）

| 文件 | 改动 | 参考 |
|---|---|---|
| `src/search_service.py` | +`XSearchProvider` 类（调 adanos `/stock/{ticker}`） | L1073 |
| `src/search_service.py` | `SearchService.__init__` 注册（复用 social key/url） | L2261/2342 |
| `src/search_service.py` | `get_search_service` 单例传 social key/url | L4451 |
| `src/search_service.py` | `search_stock_news` 增 `isinstance XSearchProvider` 分支传 `stock_code`（若采方案 a） | L3690 附近 |
| `src/core/pipeline.py` | `SearchService(...)` 构造传 social key/url | L174 |
| `tests/test_x_search.py` | **新增**测试（mock adanos `/stock/{ticker}`） | 模板 `test_anspire_search.py` |
| `docs/CHANGELOG.md` | `[Unreleased]` 扁平条目 | — |
| `.env.example` | 无需新增（复用 social 段）；可选加一行注释说明 X 复用该 key | — |

**不动**：`SocialSentimentService`、pipeline 注入逻辑、US 门禁、`score_sentiment()`、storage schema、tool registry、deep-research validator。

## 9. 可靠性

复用 `BaseSearchProvider` key 轮询/错误计数 + `SocialSentimentService` 的 tenacity 重试 + 线程安全 TTL 缓存单飞（`social_sentiment_service.py:30-150`）+ `CircuitBreaker` 熔断 + fail-open + 防御性解析。无代理/反爬（聚合服务端获取）。

## 10. 测试策略（100% 覆盖新增代码）

`tests/test_x_search.py`：mock `/x/stocks/v1/stock/{ticker}` 响应 → 测 top_tweets→SearchResult 映射、`found:false`/异常→空响应（fail-open）、ticker 解析（US/HK/A）、isinstance 分支、注册/优先级、缓存命中 + opt-in 真 API 集成测试（`SOCIAL_SENTIMENT_API_KEY` + `network` marker）。

## 11. 风险与开放项

- HK ADR 映射表需维护（可配/可查 `/search` 兜底）。
- 标准账号仅 `top_tweets`（有限）；若需全量推文需升级 Professional（`/mentions`）。
- 与 SocialSentimentService 共享 adanos 配额 → 靠 per-stock 缓存 + 熔断控制。
- 合规：复用既有 adanos（已评估），无新增风险。

## 12. 非目标（YAGNI）

❌ 不改 `score_sentiment()` · ❌ 不向 x.com 发帖 · ❌ 不做爬取/代理/反爬 · ❌ 不新建独立 sentiment 组件 · ❌ 不引入新聚合服务/新 env。

---

## 13. TDD 实现计划（RED → GREEN → REFACTOR）

> Phase 0 已完成（查证）。Phase 1 因复用 social 配置而大幅缩减。每 Phase 先测试（RED）再实现（GREEN），新增代码 100% 覆盖；不夹带无关重构。

### Phase 0 · ✅ 已完成：adanos 契约确认
- 结论：复用 `/x/stocks/v1/stock/{ticker}`（情绪 + top_tweets），标准账号可用，零新依赖。

### Phase 1 · 配置装配（极简，RED→GREEN）
- 测试：`SearchService` 在 `social_sentiment_api_key` 存在时构造 `XSearchProvider`，传入 key + url。
- 实现：`SearchService.__init__`（~L2261）+ `get_search_service`（~L4451）+ `pipeline.py:174` 复用现有 config 字段。**不新增 env**。

### Phase 2 · Provider 核心与响应映射（RED→GREEN）
- 测试：mock adanos → top_tweets 映射、`found:false`/异常→空 SearchResponse（fail-open）、市场 ticker 解析（US/HK/A）。
- 实现：`XSearchProvider`（~L1073 后）；`_do_search` 防御性解析 + 过滤。

### Phase 3 · 装配、注册与 stock_code 传入（RED→GREEN）
- 测试：注册优先级、`is_available` 随 social key 开关、`search_stock_news` 对 X 传 `stock_code`。
- 实现：注册 + `search_stock_news` 的 `isinstance` 分支（方案 a）。

### Phase 4 · 可靠性增强（RED→GREEN）
- 测试：tenacity 重试、per-stock TTL 缓存（单飞/命中）、熔断跳过。
- 实现：复用 `social_sentiment_service.py:30-150` 模式。

### Phase 5 · opt-in 真 API 集成测试
- `@pytest.mark.network` + `SOCIAL_SENTIMENT_API_KEY` 门控；CI `network-smoke` 触发。

### Phase 6 · 文档与收尾
- `docs/CHANGELOG.md` `[Unreleased]`：`- [新功能] 消息源新增 x.com（海外媒体，复用 adanos per-ticker，全市场）`。
- 复核 `.env.example`、本设计文档状态。
- 交付说明按 AGENTS.md（改了什么/为什么/验证/未验证项/风险/回滚）。

## 14. 回滚

删除 `XSearchProvider` + 注册/单例/isinstance 分支 + `pipeline.py` 传参 + 新测试 + CHANGELOG 条目。不动 `SocialSentimentService` 与任何既有路径，可独立回滚。
