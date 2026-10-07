# daily_stock_analysis 运维技能

## 项目信息

- **仓库:** `gyc567/daily_stock_analysis`
- **本地路径:** `/home/admin/code/daily_stock_analysis`
- **部署域名:** https://agentrade.space
- **核心进程:** `main.py --serve-only`(Web服务, 端口8000) + `main.py --schedule`(Scheduler)
- **自选股(14只):** `600176,688486,002957,002617,300003,300054,601208,300260,688002,603690,300623.SZ,002141,300398,688091.SH`

---

## 完整部署 + 验证流程

### Step 1: 同步远程
```bash
cd /home/admin/code/daily_stock_analysis
git fetch origin main && git pull origin main
# 如有分叉（divergent branches）:
git pull origin main --rebase
```

### Step 2: 部署
```bash
bash /home/admin/code/daily_stock_analysis/scripts/deploy-all.sh
```
该脚本自动完成：git pull → 停止旧服务 → 前端构建 → Python依赖 → 启动服务 → 健康检查 → E2E测试 → 调度检测 → 生成报告。

### Step 3: E2E 端到端测试（Python，禁止 terminal 多行 curl）

**⚠️ 用 `execute_code`（Python）进行 HTTP 测试，禁止 terminal 多行 curl（会超时阻塞）：**

```python
import urllib.request, json, ssl
ctx = ssl.create_default_context()
base = "https://agentrade.space"

def get(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
        return resp.getcode(), json.loads(resp.read())
    except:
        return None, {}

tests = [
    (base + "/health", 200),
    (base + "/api/v1/analysis/tasks", 200),
]
for url, expected in tests:
    code, d = get(url)
    ok = '✅' if code == expected else '❌'
    print(f"{ok} [{code}] {url.split('agentrade.space')[1]}")
```

### Step 4: 定时调度状态检测

**Scheduler 进程：**
```bash
pgrep -fa "main.py.*--schedule"
```

**本月每日分析统计：**
```python
import urllib.request, json, ssl
from collections import defaultdict
ctx = ssl.create_default_context()
resp = urllib.request.urlopen(
    urllib.request.Request("https://agentrade.space/api/v1/history?page_size=200",
        headers={"User-Agent": "Mozilla/5.0"}), timeout=10, context=ctx)
data = json.loads(resp.read())
by_date = defaultdict(list)
for item in data.get("items", []):
    ts = item.get("created_at","")[:10]
    if ts.startswith("2026-09"):
        by_date[ts].append(item.get("stock_code","?"))
for date in sorted(by_date.keys(), reverse=True):
    stocks = sorted(set(by_date[date]))
    print(f"{date}: {len(stocks)}只 → {stocks}")
```

**关键日志（判断非交易日跳过逻辑）：**
```bash
grep "今日休市股票已跳过" /home/admin/code/daily_stock_analysis/logs/scheduler_stdout.log | tail -1
```

**正常状态参考：**
- 工作日：14只自选股全量分析
- 周末/节假日：A股被判定休市跳过，只有创业板(300xxx)/科创板(688xxx)可能部分交易
- 周日只分析2只(300623.SZ, 688091.SH) → **正常，非Bug**

## 调度时间表

| 时间 | 执行方 | 说明 |
|------|--------|------|
| 12:00 北京时间 | 本地 Scheduler | 个股分析 |
| 21:00 北京时间 | 本地 Scheduler | 个股分析+大盘复盘 |
| 18:00 北京时间 | GitHub Actions | 完整分析 |

---

## 财务分析模块（financial_analysis）技术参考

### 数据源架构

```
fetch_fuyao_financial_series()  →  Fuyao 年报 API（24h 缓存）
    数据字段：revenue, net_profit, gross_margin（计算）, roe（计算）, debt_ratio（计算）, op_cash_flow

_score_dims()  →  四维度打分（profitability/growth/safety/valuation）
    如果 gross_margin 为 None → AkShare stock_financial_analysis_indicator 兜底取"销售毛利率(%)"
```

### Fuyao 年报序列字段（来自真实 API）

**income-statements 返回字段（已知）：**
- `fiscal_year` — 财报年度
- `operating_income` — 营业收入（✓ 有数据）
- `parent_holder_net_profit` — 归母净利润（✓ 有数据）
- `basic_eps` — 基本每股收益
- `operating_costs` — **❌ 不返回，始终为 None**

**重要：** Fuyao 年报接口不返回 `operating_costs`，所以不能用 `(revenue - cost) / revenue` 计算毛利率。毛利率必须从 AkShare 财务指标表的 `销售毛利率(%)` 字段获取。

**balance-sheets 返回字段（已知）：**
- `fiscal_year`
- `holder_equity_total` — 股东权益（用于 ROE 计算）
- `assets_total` — 资产总计（用于 debt_ratio 计算）
- `total_debt` — 负债合计（用于 debt_ratio 计算）

**计算派生字段：**
```python
gross_margin = round((rev - cost) / rev * 100, 2)  # ❌ cost 永远 None
roe         = round(np_ / equity * 100, 2)          # ✓ 有数据
debt_ratio  = round(debt / assets * 100, 2)         # ✓ 有数据
```

### AkShare 毛利率兜底

```python
import akshare as ak
df = ak.stock_financial_analysis_indicator(symbol=code, start_year=str(year))
# 列名：'销售毛利率(%)' — 注意是中文列名
gm_col = next((c for c in df.columns if "销售毛利率" in c), None)
```

**注意：** AkShare 财务指标是**季报**粒度（2024-03-31 / 2024-06-30 等），不是年报。最接近年末的季度视为年报口径。

---

## Jinja2 模板编写规范（防坑指南）

### 模板文件
- `templates/financial_analysis_report.j2` — 财务分析报告
- `templates/fundamentals_report.j2` — 基本面专项报告

### ⚠️ 坑 1：if/endif 必须严格配对

Jinja2 3.x（本项目用 3.1.6）对 `{% if %}/{% endif %}` 配对要求严格。**不能用缩进省略 endif**，每打开一个 `{% if %}` 必须有对应的 `{% endif %}`。

**验证方法（写完模板后必做）：**
```python
import re
src = open('templates/financial_analysis_report.j2').read()
depth = 0
for i, line in enumerate(src.split('\n'), 1):
    its = re.findall(r'\{%-?\s*(if|elif|else|endif)[^}]*%\}', line)
    for t in its:
        if t == 'if': depth += 1
        elif t == 'endif': depth -= 1
print(f'Final depth={depth} (should be 0)')
```
如果 `depth != 0`，说明有未关闭的 if。

### ⚠️ 坑 2：`|format()` 与 `%` 操作符冲突

`_render()` 函数使用 `md % mapping` 做 Python `%` 字符串格式化。模板中任何 `}}%` 模式（百分号紧跟双括号）会被 Python 解释器误解析。

**错误写法（会报 `not all arguments converted during string formatting`）：**
```jinja2
{{ '%.2f'|format(a.scissors) }}%
```

**正确写法：** 在 `_score_dims()` 中预格式化好字段，模板只做简单渲染：
```python
# 在 financial_analysis_service.py 的 _score_dims() 中：
scissors_fmt = f"{scissors:+.2f}" if scissors is not None else None
# 返回值包含 scissors_fmt，模板直接 {{ a.scissors_fmt }}%
```

### ⚠️ 坑 3：嵌套 if 要拆成独立行

Jinja2 的 `{% if %}` 和 `{% else %}/{% elif %}` 在同一行时，解析器行为不稳定。**每个块标签必须独占一行**：

```jinja2
{# 错误（同行的 elif 会被解析器误判）#}
{% if a %}AAA{% else %}BBB{% endif %}

{# 正确 #}
{% if a %}
AAA
{% else %}
BBB
{% endif %}
```

### ⚠️ 坑 4：模板语法报错定位

Jinja2 的 `unknown tag 'endif'` 报错指向的是**第一个无法匹配的 endif** 所在行，而非真正缺失 endif 的位置。用上面的 depth 追踪脚本定位。

---

## 财务分析服务并发去重设计

### 去重锁机制

`generate_report()` 使用两级去重：

**Level 1 — 内存锁（防并发）：**
```python
_DEDUP_LOCK: Dict[str, float] = {}  # code -> 请求到达时间戳

def _try_acquire_dedup_lock(code: str) -> bool:
    now = time.time()
    last = _DEDUP_LOCK.get(code, 0)
    if now - last < 10:  # 10 秒窗口内有请求在处理中
        return False
    _DEDUP_LOCK[code] = now
    return True
```

**Level 2 — DB 查询（防同进程重复调用）：**
```python
def _same_day_dedup_report(code: str) -> Optional[Dict[str, Any]]:
    today = date.today().isoformat()[:10]  # "2026-10-05"
    rows, _ = db.list_financial_analysis_reports(code, limit=10, offset=0)
    for r in rows:
        if str(r.get("created_at", "")).startswith(today):
            return db.get_financial_analysis_report(r["id"])  # 拿完整字段
    return None
```

**⚠️ 注意：** `list_financial_analysis_reports()` 返回的字段只有 `['id', 'stock_code', 'stock_name', 'created_at', 'health_score']`，不含 `md_path` 和 `analysis_json`，必须再查一次 `get_financial_analysis_report(id)` 拿完整记录。

### 报告 ID 格式

```python
_ID_PATTERN = "fa_{ts:%Y%m%d%H%M%S}"  # 精确到秒，避免并发碰撞
```
之前用 `%H%M`（只精确到分钟）导致同分钟多个请求时靠 DB 唯一约束兜底，产生 `_1`/`_2` 后缀文件。

---

## 已知 Bug / 历史修复记录

### 2026-10-05 财务分析模块全面修复（12项）

| # | 问题 | 严重度 | 修复文件 |
|---|------|--------|---------|
| 1 | 毛利率永远是 None（Fuyao 不返回 operating_costs） | P0 | `financial_analysis_service.py` + AkShare 兜底 |
| 2 | safety.label 被 ocf 信息污染 | P1 | `financial_analysis_service.py` |
| 3 | cash_quality 显示原始浮点数（7.408767...） | P1 | `financial_analysis_service.py` |
| 4 | profitability.label 缺失时显示混乱 | P1 | `financial_analysis_service.py` |
| 5 | PE/PB 显示原始精度（27.893924） | P1 | `financial_analysis_service.py`（预格式化字段） |
| 6 | 表格 None 显示"—%"格式 | P1 | `financial_analysis_report.j2` |
| 7 | 剪刀差对银行股语义失真（营收净利双降给正面分） | P1 | `financial_analysis_service.py` |
| 8 | years 数据模板未真正渲染 | P1 | `financial_analysis_report.j2` |
| 9 | 健康分等级说明缺失 | P1 | `financial_analysis_report.j2` |
| 10 | 模板与数据 API 不对齐 | P2 | `financial_analysis_report.j2` |
| 11 | 并发请求产生重复报告 | P2 | `financial_analysis_service.py`（内存锁） |
| 12 | ID pattern 时间精度不足（同分钟冲突） | P2 | `financial_analysis_service.py` |

### 2026-10-05 深度投研模块 Bug 修复（4项）

| # | 问题 | 严重度 | 修复文件 |
|---|------|--------|---------|
| 1 | Ownership Pydantic `List[str]` → `List[HolderInfo]`（LLM返回dict导致score=None） | P1 | `schemas/deep_research_dims.py` + `researchers/base.py` |
| 2 | 筹码 `chip_summary` 双重JSON序列化（dict→str→再次dumps） | P1 | `schemas/deep_research_dims.py`（改类型为 Dict） |
| 3 | `capital.flow_score=0` 被误判为缺失（`not 0 == True`） | P1 | `six_dim.py`（`if score is None` 先判空） |
| 4 | 筹码数据无降级策略（`chip_summary=None` 时直接None） | P2 | `six_dim.py`（`_chip_from_context()` 规则兜底） |

### 2026-09-29 深度投研幂等性修复

**两层修复：**
1. **并发去重：** `api/v1/endpoints/deep_research.py` 的 `_IN_FLIGHT_REQUESTS`
2. **报告缓存：** `src/services/deep_research_service.py` 的 `_report_cache`（30min TTL）

### 2026-09-25 Serenity Agent Timeout

**现象：** 688091.SH / 300623.SZ 在 21:00 调度中超时
**根因：** `SupplyChainReport` 的 `capacity_outlook` 中 `predicted_utilization_pct` 返回 `float` 而非 `Decimal`，Pydantic v2 校验失败走备份超时
**状态：** 已记录，待正式修复

---

## Git 同步冲突处理

```bash
git stash && git pull --rebase origin main && git stash pop
# 如遇冲突：
git checkout --theirs <conflict-file> && git add <conflict-file> && git stash drop
```

## 服务重启注意事项

- 服务启动用 `uvicorn --reload`（WatchFiles 模式），模板修改会自动重载
- 但 Jinja2 语法错误会导致服务 crash，此时必须手动重启：
  ```bash
  kill $(lsof -ti :8000) && sleep 1
  # 然后重新启动
  ```
- 健康检查：`curl http://localhost:8000/health`（或生产环境的 `https://agentrade.space/health`）
- 启动后等待 10~15 秒再发请求（WatchFiles 重载需时间）

## 深度投研 E2E 测试方法

**⚠️ 禁止用 terminal 多行 curl（会超时阻塞），用 `execute_code` Python 跑：**

```python
import urllib.request, json, time

BASE = "http://localhost:8000"  # 本地测试
# BASE = "https://agentrade.space"  # 远程测试用这个

# 财务分析 E2E（async 模式，立即返回 task_id）
req = urllib.request.Request(
    f"{BASE}/api/v1/analysis/analyze",
    data=json.dumps({
        "stock_codes": ["300054"],
        "report_type": "simple",  # simple 比 detailed 快很多
        "async_mode": True
    }).encode(),
    headers={"Content-Type": "application/json"},
    method="POST"
)
with urllib.request.urlopen(req, timeout=30) as r:
    resp = json.loads(r.read())
    task_id = resp.get("task_id")  # ← task_id 在顶层，不在 resp["data"] 里！

# 轮询状态（每5秒）
for i in range(20):
    time.sleep(5)
    with urllib.request.urlopen(f"{BASE}/api/v1/analysis/status/{task_id}", timeout=10) as r2:
        st = json.loads(r2.read())
        s = st.get("status", "?")
        print(f"[{i+1}] {s}")
        if s in ("completed", "failed", "error"):
            break
```

**关键点：**
- API 端点前缀是 `/api/v1/`（不是 `/api/`）
- `task_id` 在响应顶层 `resp["task_id"]`，不在 `resp["data"]["task_id"]`
- 深度投研 E2E 耗时长（几分钟），用财务分析 `simple` 模式代替验证

## `_researcher_score` 常见陷阱

**错误模式：**
```python
score = p.get(key)
if not isinstance(score, (int, float)):  # ← score=0 时 `not 0 == True`，错误返回 None！
    return None
```

**正确模式：**
```python
score = p.get(key)
if score is None:          # ← 先判 None
    return None
if not isinstance(score, (int, float)):
    return None
```

**为什么重要：** `score=0` 是合法值（资金流为0），不能当作"缺失数据"处理。

> **深度投研dims结构：** 详见 `references/deep_research_dims_anatomy.md` — Bug链路图、数据缺失根因。

## dims JSON 结构（易混淆）

dims JSON 最外层是 dict，不是 list：
```json
{
  "report_id": "688183_202610042104",
  "dimensions": {
    "technical": {"dim": "technical", "score": 40.0, "narrative": "..."},
    "ownership": {"dim": "ownership", "score": None, "narrative": ""},
    ...
  }
}
```
每个维度的 `dimensions.<cat>` 是一个 dict（单个维度对象），**不是 list**。
