# 套餐搜索配置

本版本停用 Tavily。遗留 `TAVILY_API_KEYS` 仅兼容读取，不参与调用或能力判断；设置页只读显示“已停用”。只读 iFinD Token 和遗留 Tavily Key 由服务端脱敏，不回传凭据原值。历史报告保留原来的来源记录。

## 本机使用智谱 Coding Plan

在项目根目录私有 `.env` 中配置：

```dotenv
CODING_PLAN_SEARCH_PROVIDER=zhipu
LLM_ZHIPU_API_KEY=<已有的智谱套餐Key>
```

已有 `LLM_ZHIPU_API_KEY` 时直接复用，不要再复制一份搜索密钥。无需 `MINIMAX_API_KEYS`，也不需要另启 MCP 服务或临时在 shell 注入变量。本地进程环境变量优先于 `.env`；修改后重启对应 Web/API、调度或 CLI 进程。设置页可查看当前选择，此项从部署配置修改。

这是两项分别配置的替代：模型继续使用现有 `LITELLM_CONFIG` / LLM 渠道配置中的智谱套餐模型入口 `https://open.bigmodel.cn/api/coding/paas/v4`；上述变量只选择联网搜索，不更改模型、推理或上下文配置。不要将现有模型路由改回普通 `api/paas/v4`。

搜索使用官方套餐 Remote MCP：`https://open.bigmodel.cn/api/mcp/web_search_prime/mcp`，Bearer 认证。官方文档称其为 `webSearchPrime`；本次真实 `tools/list` 返回的工具名是 `web_search_prime`，参数为 `search_query`、`search_recency_filter`、`content_size`。适配器使用实测名称与响应，不调用普通 Web Search API、通用 MCP broker 或模型对话内搜索。当前账号在本项目使用套餐的授权已由用户确认；路由记录本身不构成账单归属证明。

## 其他工具与线上配置

`CODING_PLAN_SEARCH_PROVIDER` 默认为 `minimax`，维持原 MiniMax Web Search 的实现、Key 和 Host。选择 `zhipu` 仅替换搜索队列中原 MiniMax 的位置：

```text
Anspire → Bocha → Brave → SerpAPI → MiniMax 或 Zhipu → SearXNG → X → 公开网页兜底
```

各工具仍按其现有配置启停和回退。SerpAPI、Bocha、Brave、SearXNG 等无需改成智谱；本地使用智谱模型也不会自动切换任何搜索工具。选择智谱而缺少 `LLM_ZHIPU_API_KEY` 时明确配置失败。鉴权、额度、MCP 业务异常、空结果或超时不会改走智谱按量地址；其余工具仍执行原来的回退规则。

普通服务启动继续复用项目入口（例如配置了项目虚拟环境后的 `python main.py --serve-only`）。本地多项目并行时，应按目标 PID 和工作目录重启，避免使用会全局终止 uvicorn/Vite 的脚本。

## 验证与回退

可用现有服务的基本面或深度投研生成功能验证，并在报告引用/搜索诊断中核对提供方 `Zhipu`、原始网站链接及日期。搜索摘要作为研究资料，不作为已核验财务数据。MCP 未给独立发布时间时保持未知；近期新闻严格过滤，业务研究资料使用原有较宽窗口，不把未知日期补成今天。

离线回归：

```bash
.venv/bin/python -m pytest tests/test_zhipu_search.py tests/test_search_comprehensive_intel.py
```

测试保存公开真实响应，替换 MCP/HTTP 传输，经过实际参数、解析、日期过滤、共享服务构造与报告上下文消费；不读取测试机密钥。真实调用受报告剩余预算和单次超时限制，无后台重试。

回退时把 `.env` 中 `CODING_PLAN_SEARCH_PROVIDER` 改回 `minimax` 并重启，恢复原 MiniMax 位置；保持原有其他工具配置。Tavily 仍停用，历史报告和原模型配置不改写。iFinD/Choice 主次单独由 [财务配置](ifind-mcp-integration.md) 控制。

官方参考：[套餐联网搜索 MCP](https://docs.bigmodel.cn/cn/coding-plan/mcp/search-mcp-server)。
