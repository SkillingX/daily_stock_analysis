# 自选股定时分析 — 用户操作指南

> 本文档面向**运维人员**，说明自选股每日自动分析功能的配置、操作和运维方法。设计原理参考 `docs/scheduled-watchlist-market-review-plan.md`。

---

## 1. 功能概述

自选股定时分析有两套并行调度机制：

| 机制 | 适用场景 | 触发时间 | 数据来源 |
|------|---------|---------|---------|
| **进程内调度器** (`main.py --schedule`) | 本地/服务器长期运行 | `WATCHLIST_ANALYSIS_TIME`（默认 09:00 北京时间） | `STOCK_LIST` 配置项 |
| **GitHub Actions Cron** | GitHub Actions 云端自动化 | UTC 0:00 = 北京时间 18:00（工作日） | `STOCK_LIST` 配置项 |

两套机制共用同一个数据源 `STOCK_LIST`，不会产生数据冲突。

---

## 2. 配置项说明

在 `.env` 中配置：

```bash
# ========== 定时任务总开关 ==========
SCHEDULE_ENABLED=true

# ========== 自选股分析 ==========
# 每日自选股自动分析时间（北京时间）
WATCHLIST_ANALYSIS_TIME=09:00

# ========== 大盘复盘 ==========
# 每日大盘自动复盘时间（北京时间）
MARKET_REVIEW_TIME=21:00

# ========== 交易 日过滤 ==========
# 启用后，非交易日不执行定时任务（按 A/H/美股日历）
TRADING_DAY_CHECK_ENABLED=true

# ========== 任务锁 ==========
# 任务锁超时时间（秒），默认 2 小时，防止进程崩溃后锁无法释放
SCHEDULE_LOCK_TIMEOUT=7200

# ========== 告警 ==========
# 任务失败时是否发送告警
SCHEDULE_ALERT_ENABLED=true
# 告警渠道：wechat / email / lark / dingtalk
SCHEDULE_ALERT_CHANNELS=wechat

# ========== 时区（勿改） ==========
TZ=Asia/Shanghai
```

> **注意**：默认 `SCHEDULE_ENABLED=false`，配置后需重启调度进程才生效。

---

## 3. 自选股数据来源

**唯一数据源是 `STOCK_LIST` 配置项**。有以下几种编辑方式：

### 3.1 Web 界面编辑（推荐）

1. 打开首页，找到**自选股 StockBar**区域
2. 点击"添加自选股"按钮，输入股票代码
3. 点击"一键分析自选股"按钮手动触发分析

编辑后自动同步到 `STOCK_LIST`，无需手动修改配置文件。

### 3.2 Web 设置页编辑

1. 进入 **Settings（设置）** 页面
2. 找到 `STOCK_LIST` 字段，直接编辑股票代码列表（逗号分隔）
3. 保存后首页自选股自动刷新

> 设置页中的 `STOCK_LIST` 与首页自选股是同一个数据源。

### 3.3 API 方式

```bash
# 获取当前自选股列表
GET /api/v1/stocks/watchlist

# 添加自选股
POST /api/v1/stocks/watchlist/add?code=600519

# 移除自选股
POST /api/v1/stocks/watchlist/remove?code=600519
```

### 3.4 手动编辑 .env

```bash
STOCK_LIST=600519,hk00700,AAPL,000858
```

编辑后：
- **进程内调度器**：下次任务执行前会自动热加载，无需重启
- **直接生效**：下次 API 调用时读取最新值

---

## 4. 进程内调度器运维（推荐方式）

### 4.1 启动调度器

```bash
python main.py --schedule
```

### 4.2 使用 systemd 管理（推荐用于生产环境）

API 与 Scheduler 独立进程需要分别使用模型时，配置 `SCHEDULER_LITELLM_CONFIG`，见 [按进程配置模型](./scheduler-model-routing.md)。CLI 启动参数保持原样；页面/API 手动触发任务仍使用 API 进程的模型。

创建 `/etc/systemd/system/dsa-scheduler.service`：

```ini
[Unit]
Description=DSA Daily Stock Analysis Scheduler
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/daily_stock_analysis
Environment=TZ=Asia/Shanghai
Environment=PATH=/path/to/daily_stock_analysis/.venv/bin:/usr/local/bin:/usr/bin
ExecStart=/path/to/daily_stock_analysis/.venv/bin/python main.py --schedule
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

启动服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable dsa-scheduler
sudo systemctl start dsa-scheduler

# 查看状态
sudo systemctl status dsa-scheduler

# 查看日志
sudo journalctl -u dsa-scheduler -f
```

### 4.3 代码更新后如何重启

```bash
# systemd 环境
sudo systemctl restart dsa-scheduler

# 直接运行的进程
pkill -f "python main.py --schedule"
python main.py --schedule &
```

**无需手动保存进度**：调度器在每次任务触发前会从数据库重新读取 `STOCK_LIST` 和最新配置，进程重启只会跳过一次计划中的任务执行，不会丢失数据。

### 4.4 调度器状态查询

```bash
# API 查询（调度器必须正在运行）
curl http://localhost:8000/api/v1/schedule/status

# 手动触发一次自选股分析
curl -X POST "http://localhost:8000/api/v1/schedule/trigger?task=watchlist"

# 手动触发一次大盘复盘
curl -X POST "http://localhost:8000/api/v1/schedule/trigger?task=market_review"
```

> 触发接口返回 `202 Accepted` + `task_id`：任务进入后台任务队列异步执行，HTTP 请求不会等待分析完成。进度可通过 `GET /api/v1/schedule/status`、`GET /api/v1/schedule/logs` 或前端 SSE 进度流观察；任务正在执行时重复触发返回 `409`。

```bash
# 查看调度日志
curl "http://localhost:8000/api/v1/schedule/logs?page=1&page_size=10"
```

### 4.5 健康检查

调度器每分钟向心跳文件写入时间戳：

```bash
# 查看心跳文件位置（基于数据库目录）
cat <db_parent>/scheduler_heartbeat
```

外部监控可以检查心跳文件是否超过 5 分钟未更新来判断调度器是否存活。

---

## 5. GitHub Actions 调度运维

### 5.1 触发方式

GitHub Actions `00-daily-analysis.yml` 支持两种触发方式：

| 触发方式 | 说明 |
|---------|------|
| **Cron 自动** | UTC 0:00 = 北京时间 18:00（周一至周五） |
| **workflow_dispatch（手动）** | 在 GitHub Actions 页面手动触发，可选模式：full / market-only / stocks-only |

### 5.2 手动触发

在 GitHub Actions 页面：
1. 进入 **Actions** 标签
2. 选择 **00-daily-analysis** 工作流
3. 点击 **Run workflow**
4. 选择运行模式：
   - `full`：自选股分析 + 大盘复盘
   - `stocks-only`：仅自选股分析
   - `market-only`：仅大盘复盘

### 5.3 与进程内调度器的冲突风险

> **重要**：如果同时运行进程内调度器和 GitHub Actions，两者都会执行分析任务。

建议：
- **仅使用进程内调度器**：在 `.env` 中设置 `SCHEDULE_ENABLED=true`
- **仅使用 GitHub Actions**：确保本地没有运行 `main.py --schedule`
- **同时使用**：确认两者的执行时间不重叠，且都开启了交易日过滤

---

## 6. 重新部署代码后的操作

### 6.1 是否需要重启调度器？

| 变更类型 | 是否需要重启 | 说明 |
|---------|:-----------:|------|
| Python 代码更新 | **需要** | 必须重启才能加载新代码 |
| `.env` 配置变更 | 不需要 | 调度器在每次任务执行前会自动热加载 |
| `STOCK_LIST` 变更（通过 API/设置页） | 不需要 | 同上 |
| 前端代码更新 | 不需要 | 不影响后端调度器 |

### 6.2 重启步骤

**systemd 环境**：
```bash
sudo systemctl restart dsa-scheduler
```

**直接运行的进程**：
```bash
pkill -f "python main.py --schedule"
python main.py --schedule &
```

### 6.3 验证重启成功

```bash
# 检查进程是否存活
ps aux | grep "main.py --schedule"

# 检查心跳文件更新时间
stat <db_parent>/scheduler_heartbeat

# 通过 API 确认状态
curl http://localhost:8000/api/v1/schedule/status
```

---

## 7. 故障排查

### 7.1 任务没有按计划执行

1. **检查调度器是否在运行**：
   ```bash
   ps aux | grep "main.py --schedule"
   ```

2. **检查 `.env` 配置**：
   ```bash
   grep "SCHEDULE_ENABLED\|WATCHLIST_ANALYSIS_TIME\|MARKET_REVIEW_TIME" .env
   ```
   确认 `SCHEDULE_ENABLED=true` 且时间格式正确（如 `09:00`）。

3. **检查是否为非交易日**：
   ```bash
   grep "TRADING_DAY_CHECK_ENABLED" .env
   ```
   如果 `TRADING_DAY_CHECK_ENABLED=true`，节假日不会执行任务。

4. **检查任务锁是否卡住**：
   ```bash
   ls -la <db_parent>/locks/
   cat <db_parent>/locks/watchlist_analysis.lock
   ```
   如果锁文件存在且 PID 不存在，说明锁可能卡住了。可以手动删除锁文件：
   ```bash
   rm <db_parent>/locks/watchlist_analysis.lock
   ```

5. **检查调度日志**：
   ```bash
   curl "http://localhost:8000/api/v1/schedule/logs?page=1&page_size=20"
   ```

### 7.2 任务重复执行

原因：可能同时运行了进程内调度器和 GitHub Actions。

解决方法：
1. 检查 GitHub Actions 是否有多余的 workflow_dispatch 触发
2. 确认本地只有一个调度器进程
3. 检查是否有多个 Docker 容器或多个 systemd 服务实例在运行

### 7.3 报告没有生成

1. **检查 `reports/` 目录权限**
2. **检查 `STOCK_LIST` 是否为空**：
   ```bash
   curl http://localhost:8000/api/v1/stocks/watchlist
   ```
3. **检查单只股票分析是否正常**（手动触发一只股票的分析）

### 7.4 心跳文件不更新

1. 确认调度器进程没有崩溃：
   ```bash
   ps aux | grep "main.py --schedule"
   ```
2. 查看调度器日志：
   ```bash
   tail -100 logs/backend.log
   ```

---

## 8. 回滚方案

| 场景 | 回滚操作 |
|------|---------|
| 定时任务异常/费用激增 | 设置 `SCHEDULE_ENABLED=false`，然后 `sudo systemctl restart dsa-scheduler` |
| 任务重复执行 | 清理锁文件：`rm <db_parent>/locks/*.lock`，然后重启调度器 |
| 报告丢失 | 从 `reports/archive/` 恢复历史归档版本 |
| 时区错误 | 检查 `TZ=Asia/Shanghai` 是否生效，修正后重启调度器 |
| 新代码导致崩溃 | 回滚代码，重新部署旧版本，重启调度器 |

---

## 9. 关键文件索引

| 文件路径 | 说明 |
|---------|------|
| `src/scheduler.py` | 调度器核心实现 |
| `main.py:1309–1364` | 自选股定时任务回调 |
| `main.py:1390–1454` | 大盘复盘定时任务回调 |
| `src/core/scheduled_task_lock.py` | 任务锁实现 |
| `src/core/trading_calendar.py` | 交易日历判断 |
| `api/v1/endpoints/schedule.py` | 调度状态/触发 API |
| `api/v1/endpoints/stocks.py:331–417` | 自选股 CRUD API |
| `.github/workflows/00-daily-analysis.yml` | GitHub Actions 调度 |
| `docs/scheduled-watchlist-market-review-plan.md` | 设计文档（面向开发者） |
