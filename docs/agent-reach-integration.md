# AgentReachService 接入指南

> **状态更新（2026-10，issue #46）**：雪球渠道（热帖/报价/搜索/热榜）与依赖它的社区评价能力已退役，`AgentReachService` 已删除；本文保留为历史集成说明。网页正文阅读由 `src/search_service.fetch_url_content` 独立保留。


> 封装 Agent-Reach web（Jina Reader）+ 雪球（官方 API + cookie）双渠道，统一输出 frozen dataclass。`agent_reach` 为可选外部依赖，**不配置也可运行，配置后增强能力**，符合本仓库稳定性护栏。

## 外部前置条件

```bash
# 安装 Agent-Reach（CLI + 路由器）
pipx install agent-reach
agent-reach install

# 雪球渠道需要 cookie（可选，不配则雪球渠道 warn/off，不影响 web 渠道）
agent-reach configure --from-browser chrome
# 或手动写入 ~/.agent-reach/config.yaml:
#   xueqiu_cookie: "your_xueqiu_cookie_here"
```

| 渠道 | 依赖 | 免费 tier | 说明 |
|---|---|---|---|
| Web（Jina Reader）| 无 | ✅ 零配置恒可用 | `GET https://r.jina.ai/{url}`，`Accept: text/plain`，30s 超时 |
| 雪球 | `xueqiu_cookie` | ⚠️ 需登录 cookie | 官方 API（`stock.xueqiu.com`），cookie 优先级：config.yaml → browser cookie3 → 首页兜底 |

**`requirements.txt` 不声明 `agent_reach` 依赖**（外部前置条件，文档说明即可）。

## 服务 API

```python
from src.services.agent_reach_service import AgentReachService

svc = AgentReachService()

# 可用性
svc.is_available()       # bool：agent_reach 包是否已安装
svc.unavailable_reason() # str | None：不可用原因

# 健康检查
svc.health_check()  # dict[str, ChannelStatus]
#   -> {"web": ChannelStatus(name="web", available=True, status="ok", backend="Jina Reader", message="ok"),
#        "xueqiu": ChannelStatus(name="xueqiu", available=True|False, status="ok"|"warn"|"off", ...)}
svc.is_available()       # bool：agent_reach 包是否已安装
svc.unavailable_reason() # str | None：不可用原因

# 健康检查
svc.health_check()  # dict[str, ChannelStatus]
#   -> {"web": ChannelStatus(...), "xueqiu": ChannelStatus(...)}

# Web 渠道（Jina Reader）
svc.read_url("https://example.com/article")
#   -> ContentItem(platform="web", title="...", url="...", content="...", snippet="...", source="Jina Reader", ...) | None

# 雪球渠道
svc.get_xueqiu_hot_posts(limit=20)          # -> list[ContentItem]（平台="xueqiu"）
svc.get_xueqiu_stock_quote("SH600519")      # -> XueqiuQuote(...) | None
svc.search_xueqiu_stocks("贵州茅台", limit=10) # -> list[XueqiuStock]
svc.get_xueqiu_hot_stocks(limit=10)          # -> list[XueqiuHotStock]
```

## 标准化数据结构

```python
ContentItem(platform, title, url, content, snippet, source, author, published_at, fetched_at)
XueqiuQuote(symbol, name, current, percent, chg, high, low, open, last_close, volume, amount, pe_ttm, timestamp, market_capital, turnover_rate)
XueqiuStock(symbol, name, exchange)
XueqiuHotStock(symbol, name, current, percent, rank)
ChannelStatus(name, available, status, backend, message)
AgentReachError  # 基异常
AgentReachUnavailableError(AgentReachError)  # 未安装时抛出
```

## 降级行为（fail-open）

| 场景 | 返回 | 说明 |
|---|---|---|
| `agent_reach` 未安装 | `is_available()=False`，各方法返回 `None`/`[]` | 不抛异常，不拖垮主流程 |
| Jina Reader 失败 | `read_url()` → `None` | 记 warning |
| 雪球 cookie 未配 | `get_xueqiu_*()` → `[]` 或 `None` | `health_check()` 反映 `status="warn"` |
| 雪球 API 异常 | 各方法 → `None`/`[]` | 记 warning |

## 接入下游（后续任务）

本服务作为公共底层能力就位，消费方接入留作后续独立任务：

- **问股工具**：接入 `read_url` 读个股公告/研报 URL
- **供应链分析**：接入 `get_xueqiu_hot_posts` 读雪球相关讨论
- **深度投研**：接入 `get_xueqiu_stock_quote` 读实时行情数据
- **搜索增强**：接入 `search_xueqiu_stocks` 辅助股票搜索

接入下游时，只需实例化 `AgentReachService()` 并调用对应方法，无需关心 `agent_reach` 是否已安装——fail-open 保证任何情况下都不拖垮主流程。

## 回滚

删除 `src/services/agent_reach_service.py` + `tests/test_agent_reach_service.py` + CHANGELOG 条目即可。零副作用，无配置项，无下游依赖。
