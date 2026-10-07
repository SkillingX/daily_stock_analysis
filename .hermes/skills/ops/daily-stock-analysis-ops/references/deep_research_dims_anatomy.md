# 深度投研 dims JSON 结构解析（2026-10-05 诊断成果）

## dims JSON 正确结构

最外层是 **dict**，不是 list：
```json
{
  "report_id": "688183_202610042104",
  "engine": "dual_track",
  "guardrail_events": [],
  "dimensions": {
    "technical": {"dim": "technical", "score": 40.0, "narrative": "..."},
    "ownership": {"dim": "ownership", "score": None, "degraded_reason": "..."},
    "capital": {"dim": "capital", "score": 55.0, "chip_summary": {...}},
    ...
  }
}
```

**不要**按 list 方式遍历 `for d in dims`，要按 dict：`dims["ownership"]["score"]`

## 各维度数据质量（18只股票统计）

| 维度 | 有效得分/总数 | 备注 |
|------|-------------|------|
| technical | 1/18 | 缠论+均线+RSI，正常 |
| sentiment | 1/18 | 情绪舆情，正常 |
| business | 1/18 | 业务分析，正常 |
| us_china | 1/18 | 中美关系，正常 |
| ownership | 0/18 | ❌ Bug1: Pydantic List[str] 报错 |
| capital | 0/18 | ❌ Bug2+3: 双重序列化+flow_score=0 |
| sector | 0/18 | 数据源未接入 |
| fundamental | 0/18 | 营收/净利增速缺失（2025年报未发布）|

## Bug 链路图

### Bug1: Ownership score=None
```
OwnershipDim.top_holders: List[str]  ← schema定义
↓
LLM 返回: [{"name": "广东生益科技", "source": "news"}, ...]  ← 实际是 dict
↓
Pydantic 报错: Input should be a valid string
↓
generic_parse 降级: score=None, narrative=""
```

### Bug2: chip_summary 双重序列化
```
CapitalDim.chip_summary: str  ← schema定义
↓
LLM 返回: {"avg_cost": 16.5, "profit_ratio": 0.18, ...}  ← 实际是 dict
↓
generic_parse: json.dumps(dict) → 字符串
↓
deep_research_service: model_dump() → 再次 json.dumps() → 双重转义
↓
文件里: "{\"avg_cost\": 16.5, ...}"  ← 字符串里的字符串
```

### Bug3: flow_score=0 被跳过
```
_researcher_score({"status": "ok", "flow_score": 0.0}, "flow_score")
↓
score = p.get("flow_score")  → 0.0
↓
if not isinstance(0.0, (int, float)):  → False（0.0是float）
↓
但前面漏了: if not isinstance(score, (int, float)):  ← 0.0进不来
↓
实际是: if not isinstance(score, (int, float)): return None  ← score=0时 not 0 == True！
```

## 数据缺失根因汇总

| 数据 | 缺失频率 | 根因 |
|------|---------|------|
| 营收/净利增速 | 50% | 2025年报未发布（2026-10） |
| 板块实时排行 | 50% | AkShare 板块排行接口未接入 |
| ROE/毛利率 | 44% | Fuyao 缺字段，AkShare 季报口径不一致 |
| 估值 PE/PB | 28% | 非交易日返回 None |
| 资金流 | 100% | push2his 接口大陆访问超时 |
