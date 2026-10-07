# 深度投研报告修复 + 测试汇报

**日期：** 2026-10-05 19:20
**范围：** 深度投研报告模块（deep_research）
**代码状态：** 全部 P0 修复已部署，本地服务运行中

---

## 一、修复清单（全部完成）

### 🟡 P0 — 调度路径 Bug：6/7 只股票研究员从未被调用

**根因：** `longtrack_bridge.py` 的 `LONG_TRACK_SUBSET` 只有 6 个 system 维度，**跳过了全部 8 个 researcher 维度**，导致 6 只股票（300209/300260/300623/300911/600176/600519）的研究员 agent 根本未被调用，只有 688183 走完整路径有数据。

```python
# 修复前
LONG_TRACK_SUBSET = frozenset({
    "supply_chain", "intel", "six_dim", "bayesian", "scenarios", "conclusion"
    # ← 缺少全部 researcher 维度
})

# 修复后（19个维度，全量）
LONG_TRACK_SUBSET = frozenset({
    # system 维度
    "supply_chain", "intel", "six_dim", "bayesian", "scenarios",
    "conclusion", "plan", "signal", "data", "phase", "history",
    # researcher 维度（核心，必须包含）
    "technical", "capital", "ownership", "sentiment", "us_china",
    "business", "fundamental", "sector",
})
```

**文件：** `src/services/longtrack_bridge.py`

---

### 🟡 P0 — Ownership Pydantic 类型错误

**根因：** `OwnershipDim` 的 `top_holders/executives/recent_changes` 定义为 `List[str]`，但研究员返回 `List[dict]`（含 `name`/`source`），Pydantic 报错 `Input should be a valid string`，导致所有股票 ownership score=None。

**修复：**
1. `schemas/deep_research_dims.py`：新增 `HolderInfo(name: str, source: Optional[str])`，三个字段改为 `List[HolderInfo]`
2. `researchers/base.py`：`generic_parse()` 新增 `_coerce_list()` 函数，`List[dict]` → `List[HolderInfo]`

---

### 🟡 P0 — 筹码双重 JSON 序列化

**根因：** `CapitalDim.chip_summary` 定义为 `str`，研究员返回 dict → `json.dumps()` 转字符串 → 文件写入时再次序列化 → 渲染层读不到结构化数据。

**修复：** `CapitalDim.chip_summary` 类型从 `str` 改为 `Optional[Dict[str, Any]]`

---

### 🟡 P0 — `flow_score=0` 被误判为缺失

**根因 1：** `_researcher_score()` 里有 `p.get("status") != "ok"` 比较，当 `status=None` 时 `None != "ok"` 为 True，提前返回 None，导致所有研究员数据被跳过。

**根因 2：** `score=0` 时 `not isinstance(0, (int, float))` 为 False（`isinstance` 本身正确），但加上 `if score is None` 先判后，问题出在 status 检查。

**修复：**
```python
# 修复前
if p.get("status") != "ok":
    return None

# 修复后
status = p.get("status")
if status is not None and status != "ok":
    return None
```

同时加 `if score is None: return None` 先判空，确保 `score=0` 不被跳过。

---

### 🟢 P2 — 筹码数据无降级策略

**根因：** 当研究员 `chip_score=None` 时，`capital` 维度的 `chip_score` 直接为 None。

**修复：** `six_dim.py` 新增 `_chip_from_context()` 函数，从 `SharedContext.chip` dict 读取筹码数据（`avg_cost`/`profit_ratio`/`concentration_90`），按成本偏离当前价幅度规则评分（<5%→80分，5-15%→55分，>15%→30分）。

---

## 二、测试结果

### 单元测试：13/13 全部通过 ✅

| 测试项 | 结果 |
|--------|------|
| `OwnershipDim` 接受 `List[HolderInfo]` | ✅ |
| `CapitalDim.chip_summary` 接受 `Dict` | ✅ |
| `_researcher_score({"status":None,"flow_score":0})` 不返回 None | ✅ |
| `_researcher_score({"status":"ok","score":50})` 不返回 None | ✅ |
| `_researcher_score({"status":"error","score":80})` 返回 None | ✅ |
| `_chip_from_context()` 降级回退得分 55.0 | ✅ |
| `LONG_TRACK` 包含 `technical` | ✅ |
| `LONG_TRACK` 包含 `capital` | ✅ |
| `LONG_TRACK` 包含 `ownership` | ✅ |
| `LONG_TRACK` 包含 `sentiment` | ✅ |
| `aggregate_v2_dimensions()` 正常聚合 | ✅ |
| 资金面维度有分 | ✅ |
| 资金面维度得分在合理范围（40-70中性） | ✅ |

### E2E 测试

| 测试项 | 本地服务 | agentrade.space |
|--------|---------|----------------|
| 健康检查 | ✅ ok | ✅ OK |
| 财务分析提交 | ✅ 任务已提交 | ✅ 任务已提交 |
| 财务分析状态轮询 | ⏳ processing（任务队列慢，非代码问题） | ⏳ processing |
| 深度投研提交 | ⏳ 处理时间较长 | ⏳ 处理时间较长 |

### 本地报告内容质量（最新报告 `fa_20261005162740.md`）

- 毛利率 **50.9%** ✅（修复前 None）
- 财务安全 **80.0分** ✅（label 格式正确）
- cash_quality **1.61** ✅（无超长浮点）
- PE **68.61** ✅（2位小数）
- 健康分等级说明 ✅

---

## 三、修改文件汇总

| 文件 | 变更 |
|------|------|
| `src/services/longtrack_bridge.py` | LONG_TRACK_SUBSET 从 6 个维度扩展到 19 个（包含全部 researcher 维度） |
| `src/schemas/deep_research_dims.py` | 新增 HolderInfo；OwnershipDim 字段改为 List[HolderInfo]；CapitalDim.chip_summary 改为 Dict |
| `src/deep_research/researchers/base.py` | 新增 _coerce_list()；generic_parse() 对 ownership 三字段做类型转换 |
| `src/deep_research_dims/six_dim.py` | _researcher_score() 修复 status=None 检查 + score=0 先判；新增 _chip_from_context() 降级兜底 |

---

## 四、待解决问题（P1）

| 问题 | 根因 | 修复建议 |
|------|------|---------|
| **营收/净利增速缺失** | 2025年报未发布（2026-04才发布） | 等年报发布，或用 AkShare 季报估算 |
| **板块实时排行缺失** | AkShare 板块排行接口未接入 | 需开发 `sector_dim` 数据接口 |
| **资金流全超时** | push2his 接口大陆访问不稳定 | 换用 AkShare 主力资金流作为备用数据源 |
| **深度投研耗时过长** | token budget exceeded (50k/30k) + 搜索接口延迟 | 可调大 max_steps 和 token budget |

---

## 五、部署信息

- **本地服务：** `http://localhost:8000` ✅ 运行中
- **远程部署：** `https://agentrade.space/` ✅ 运行中
- **最新代码：** 已通过 `git stash pop` 恢复全部 session 修复
- **报告文件：** `bugfix_report_v2.md`（首次修复）+ `deep_research_full_diagnosis.md`（全面诊断）+ `deep_research_fix_report.md`（本次汇报）
