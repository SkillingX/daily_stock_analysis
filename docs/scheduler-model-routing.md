# API 与 Scheduler 分别使用模型

适用于 API 与 Scheduler 独立进程部署。两者共用业务 `.env`，API 沿用
`LITELLM_CONFIG` / `LLM_CHANNELS` / legacy 模型配置，Scheduler 可单独指定模型 YAML。
本机 API 可保留 GLM 等开发配置，线上 API 可继续使用 MiniMax。

## 配置

在项目根目录私有 `.env`（或当前 `ENV_FILE`）中新增：

```dotenv
LLM_KIMI_API_KEY=填写Kimi_Code控制台创建的套餐Key
SCHEDULER_LITELLM_CONFIG=docs/examples/litellm_kimi_code.example.yaml
```

密钥不进入 YAML。示例 YAML 使用 `https://api.kimi.com/coding/v1`、
模型 ID `k3.1-pro` 和 `low` 推理强度。K3.1 Pro 只支持 `low`、`high`、`max`；
Kimi 将第三方请求的 `medium` 映射为 `high`。按账号地域和模型权益修改 YAML；
海外入口为 `https://api.kimi.ai/coding/v1`。
Kimi Code 套餐 Key 与 Moonshot 开放平台按量计费 Key 不通用。
后台批量调用须符合账号实际开通范围；本次接入的后台调用范围已由项目方确认。
接口、模型及 Key 创建入口见 [Kimi 官方说明](https://www.kimi.com/en/help/kimi-code/membership-guide)。

Scheduler YAML 必须只有一个 deployment，包含非空 `model_name`、
`litellm_params.model`、HTTP(S) `api_base` 和有效密钥引用。
该 deployment 同时用于 Scheduler 的普通分析和 Agent，备用模型列表为空。
配置文件缺失、为空、多个 deployment 或密钥未解析时明确失败，避免退回 API 模型。
未设置 `SCHEDULER_LITELLM_CONFIG` 时保留原有行为。

## 启动与重载

使用现有虚拟环境分别启动，CLI 参数无需新增：

```bash
python main.py --serve-only --host 0.0.0.0 --port 8000
python main.py --schedule --no-run-immediately
```

实际部署可继续通过现有 nohup 或进程管理入口运行这两条命令。
启用专用模型时拒绝同时在一个进程中启动 Web 与 Scheduler，避免一个进程内的全局模型混用。
`--serve-only` 进程不启用 Scheduler 专用模型，即使共享配置设置了 `SCHEDULE_ENABLED=true`。

Scheduler 启动及每次任务执行前都加载专用配置，业务 `.env` 的重载继续生效。
启动日志显示 `Scheduler LLM: model=..., source=litellm_config`，不输出 Key。
修改模型、地址、Key 后重启实际 Scheduler 进程，以重建已缓存的客户端；
Web 设置页的普通模型设置只管理 API 模型，不管理 Scheduler YAML。
两侧的模型并发额度仍分别在各自进程内计算。

分流按执行进程决定：定时自动触发使用 Scheduler 模型；页面/API 手动触发
`/api/v1/schedule/trigger` 当前在 API 进程执行，因此使用 API 模型。
搜索服务、Choice、iFinD、任务时间、通知和报告规则沿用各自现有配置。

## 验证与回滚

本地确定性检查使用模拟供应商和虚拟 Key，不调用付费接口：

```bash
python -m pytest tests/test_scheduler_llm_routing.py -q
```

检查独立进程请求去向、任务重载、模型拒绝时不跨渠道回退、无效专用配置明确失败，
以及未启用专用模型时的兼容行为。真实验收分别从 API 与 Scheduler 执行一项业务任务，
核对日志中的模型及供应商控制台用量；接口路由成功本身不替代套餐用量核对。

已有模型诊断入口也可单独验证 Scheduler，以下命令会实际请求模型，
只输出模型、结束原因等脱敏元数据，不执行请求中的工具：

```bash
python scripts/probe_agent_model.py request.json --scheduler --timeout 120
```

`request.json` 沿用该脚本原有的 `messages` / `tools` JSON 格式；不加
`--scheduler` 则验证普通 API 模型配置。它验证模型链路，不替代完整业务报告验收。

回滚时删除或清空 `SCHEDULER_LITELLM_CONFIG`，重启 Scheduler，恢复共用全局模型配置。
API 配置无需调整；无需修改数据库或报告历史。
