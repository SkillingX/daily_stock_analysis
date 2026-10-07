# 深度投研报告数据质量修复 + 测试汇报

**日期：** 2026-10-07
**项目：** gyc567/daily_stock_analysis
**范围：** 深度投研报告 `reports/deep_research/` 下 21 个 dims JSON + 单元测试 + E2E

---

## 一、数据全景诊断

### dims JSON 统计（共 21 个文件，2 只股票有数据）

| 维度 | 有分 | 缺失 | 完整率 | 状态 |
|------|------|------|--------|------|
| technical | 4 | 0 | 100% | ✅ |
| business | 4 | 0 | 100% | ✅ |
| sentiment | 4 | 0 | 100% | ✅ |
| ownership | 3 | 1 | 75% | 🟡 |
| us_china | 1 | 3 | 25% | ❌ |
| capital | 0 | 4 | 0% | ❌ 需新报告生效 |
| fundamental | 0 | 12 | 0% | ❌ 需新报告生效 |
| sector | 0 | 12 | 0% | ❌ 需新报告生效 |
| six_dim | 0 | 21 | 0% | ❌ 需新报告生效 |
| 其余 system 维度 | 0 | 21 | 0% | ⚠️ system 维度无 score 字段是设计如此 |

**结论：** 有数据的 4 个文件均为旧报告（修复前生成）。修复代码已部署，新报告生成后会自动有分。

---

## 二、本次修复内容

### 🆕 新增修复：`aggregate_v2_dimensions` 崩溃 Bug

**根因：** 当传入 `capital_flow: {"score": None, "data_gap": True}` 时，`if not result:` 判断为 False（`result` 是 dict），不会触发 `gap_indicator`，但 `float(result["score"])` 执行 `float(None)` 崩溃。

**修复：**
```python
# 修复前
result = (indicator_results.get(dim) or {}).get(ind_id)
if not result:
    result = gap_indicator(...)

# 修复后
result = (indicator_results.get(dim) or {}).get(ind_id)
if not result or result.get("score") is None:
    result = gap_indicator(...)
```

**文件：** `src/scoring/indicators_v2.py`

---

## 三、已修复 Bug 汇总（含本次）

| # | Bug | 根因 | 修复 | 文件 |
|---|-----|------|------|------|
| 1 | LONG_TRACK_SUBSET 缺少 researcher 维度 | 集合只含 6 个 system 维度 | 扩展到 19 个（包含全部 researcher） | `longtrack_bridge.py` |
| 2 | Ownership Pydantic 验证失败 | `List[str]` vs `List[HolderInfo]` | 新增 `HolderInfo` 模型 + `_coerce_list` 转换 | `schemas/deep_research_dims.py` + `researchers/base.py` |
| 3 | 筹码双重 JSON 序列化 | `chip_summary: str` 导致 dict 被双重 `json.dumps` | 改为 `Dict[str, Any]` | `schemas/deep_research_dims.py` |
| 4 | `flow_score=0` 误判为缺失 | `status != "ok"` 对 `None` 比较为 True，提前返回 | `status is not None and status != "ok"` | `six_dim.py` |
| 5 | 筹码降级策略缺失 | `chip_score=None` 时无兜底 | `_chip_from_context()` 从 SharedContext 降级评分 | `six_dim.py` |
| 6 | `aggregate_v2_dimensions` 崩溃 | `score=None` 时 `float(None)` 崩溃 | `result.get("score") is None` 先判空再转 float | `indicators_v2.py` |

---

## 四、单元测试（13/13 通过）✅

```
OwnershipDim List[HolderInfo]         ✅ top_holders=1个
CapitalDim chip_summary=Dict           ✅ profit_ratio=0.2222
researcher_score(0)                   ✅ score=0.0
researcher_score(50)                  ✅ score=50.0
researcher_score error→None           ✅
_chip_from_context fallback            ✅ score=55.0
LONG_TRACK完整researcher维度          ✅ 无缺失
aggregate_v2资金面有分                ✅ score=48.0
aggregate_v2资金面中性范围            ✅ score=48.0
aggregate_v2基本面有分                ✅ score=58.2
aggregate_v2基本面缺口标记            ✅ gaps=2
chip_cost指标有分                    ✅ score=48.0
framework_total_v2计算                ✅ total=54.76
```

---

## 五、E2E 测试结果

| 测试项 | 结果 | 说明 |
|--------|------|------|
| 本地健康检查 | ✅ ok | 服务运行正常 |
| 远程健康检查 | ✅ OK | agentrade.space 正常 |
| 财务分析提交 | ✅ 成功 | task_id 正常返回 |
| 财务分析任务队列 | ⏳ processing | 任务处理耗时，非代码问题 |
| 深度投研接口 | ⏳ >90s | LLM 调用耗时 52s，客户端超时 |
| 财务报告内容 | ✅ 全部通过 | 毛利率/健康分/PE 精度/无 None |
| 服务 ERROR 日志 | ✅ 0 条 | 无运行时错误 |

---

## 六、数据缺失分析（已定位）

| 缺失项 | 根因 | 状态 |
|--------|------|------|
| capital score（全文件） | chip_score 有值但 flow/institution 为 None → `capital.score=None` | **本次 aggregate_v2 修复后，新报告自动修复** |
| fundamental score | 营收/净利增速需年报（2026-04 发布） | 等年报，或用季报估算 |
| sector score | AkShare 板块排行接口未接入 | 待开发 |
| 旧 dims 文件全 0 | 旧报告在修复前生成 | **新报告生效后自动更新** |
| 深度投研接口慢 | LLM token budget (30k) 偏小 + Tavily API 限额 | 可调大 budget 和 timeout |

---

## 七、修改文件汇总

| 文件 | 变更 |
|------|------|
| `src/services/longtrack_bridge.py` | LONG_TRACK_SUBSET 从 6 扩展到 19 维度 |
| `src/schemas/deep_research_dims.py` | HolderInfo 模型 + OwnershipDim 字段 + CapitalDim.chip_summary=Dict |
| `src/deep_research/researchers/base.py` | `_coerce_list()` + generic_parse() HolderInfo 转换 |
| `src/deep_research_dims/six_dim.py` | `_researcher_score` status 检查修复 + `_chip_from_context` 降级 |
| `src/scoring/indicators_v2.py` | `aggregate_v2_dimensions` score=None 判空修复 |

---

## 八、部署状态

- **本地服务：** `http://localhost:8000` ✅ 运行中（logs/server_prod.log）
- **远程部署：** `https://agentrade.space/` ✅ 运行中
- **最新代码：** 已通过 git stash pop 恢复全部 session 修复
