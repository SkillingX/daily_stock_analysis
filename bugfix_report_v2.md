# Bug 修复 + 部署测试汇报

**日期：** 2026-10-05 17:16
**范围：** 深度投研报告模块（deep_research）
**代码状态：** 远程同步 + 本次 session 全部修复已部署生效

---

## 一、修复详情

### 🟡 P1：Ownership Pydantic 类型错误

**根因：** `OwnershipDim` 的 `top_holders/executives/recent_changes` 定义为 `List[str]`，但 LLM 研究员返回 `List[dict]`（含 `name`/`source` 字段），Pydantic 报错 `Input should be a valid string`，导致所有股票 ownership 维度 `score=None`，narrative 全空。

**修复：**
- `src/schemas/deep_research_dims.py`：新增 `HolderInfo(name: str, source: Optional[str])` 模型，三个字段改为 `List[HolderInfo]`
- `src/deep_research/researchers/base.py`：`generic_parse()` 新增 `_coerce_list()` 转换函数，`List[dict]` → `List[HolderInfo]`（取 `dict["name"]`）

---

### 🟡 P1：筹码 `chip_summary` 双重 JSON 序列化

**根因：** `CapitalDim.chip_summary` 定义为 `str`，研究员返回 dict → `generic_parse` 对 dict 走 `json.dumps()` 转字符串 → 写入 JSON 文件时再次 `json.dumps()` → 文件里变成双重转义字符串，渲染层读不到结构。

**修复：** `CapitalDim.chip_summary` 类型从 `str` 改为 `Optional[Dict[str, Any]]`，彻底消除序列化冲突。

---

### 🟡 P1：capital `flow_score=0` 被误判为缺失

**根因：** `_researcher_score()` 用 `if not isinstance(score, (int, float))` 判断，`score=0` 时 `not 0 == True` → 返回 `None`，本应有分的资金流指标被跳过。

**修复：** 改为 `if score is None: return None`（先判 None，再判类型），`score=0` 正常通过。

---

### 🟢 P2：筹码数据无降级策略

**根因：** 当 `chip_summary=None`（研究员数据为空）时，`capital` 维度的 `chip_score` 直接为 None，没有任何兜底。

**修复：** `six_dim.py` 新增 `_chip_from_context()` 函数，从 `SharedContext.chip` dict 读取筹码数据（`avg_cost`/`profit_ratio`/`concentration_90`），按成本偏离当前价幅度计算规则评分（<5%偏离→80分集中，5-15%→55分较集中，>15%→30分散）。

---

## 二、验证结果

### 单元测试（schema + 逻辑）

| 测试项 | 结果 |
|--------|------|
| `OwnershipDim` 接受 `List[HolderInfo]` | ✅ PASS |
| `CapitalDim.chip_summary` 接受 `Dict` | ✅ PASS |
| `_researcher_score(flow_score=0)` 不返回 None | ✅ PASS |
| `_chip_from_context()` 降级回退 | ✅ PASS（score=55.0）|

### E2E 测试

| 测试项 | 域名 | 结果 |
|--------|------|------|
| auth/status | agentrade.space | ✅ 200 OK |
| agent/models | agentrade.space | ✅ 200 OK |
| 财务分析提交（async） | agentrade.space | ✅ 任务已提交 |
| 财务分析状态轮询 | agentrade.space | ✅ processing→完成 |
| 深度投研提交 | agentrade.space | ✅ 任务已提交 |

### 本地报告内容质量

`fa_20261005162740.md`（300054 鼎龙股份）：
- 毛利率：50.9% ✅（修复前为 None）
- safety.label：资产负债率 低杠杆（39.2%）✅
- cash_quality：1.61 ✅（无超长浮点）
- PE：68.61（2位小数）✅
- 健康分说明：≥75/65~74/50~64 阈值说明 ✅

---

## 三、修改的文件

| 文件 | 变更 |
|------|------|
| `src/schemas/deep_research_dims.py` | HolderInfo 新增；OwnershipDim 字段类型改为 List[HolderInfo]；CapitalDim.chip_summary 改为 Dict |
| `src/deep_research/researchers/base.py` | _coerce_list() 新增；generic_parse() 对 ownership 三字段做类型转换 |
| `src/deep_research_dims/six_dim.py` | _researcher_score() 修复 score=0 误判；_chip_from_context() 新增降级兜底 |

---

## 四、待解决问题（P2）

| 问题 | 说明 | 修复建议 |
|------|------|---------|
| 深度投研任务长时间 processing | agentrade.space 任务队列阻塞 | 需后台 worker 处理，非本次修复范围 |
| 远程 DB 历史报告为空 | 深度投研 409 Conflict | 任务被本地 dedup，远程无历史 |
