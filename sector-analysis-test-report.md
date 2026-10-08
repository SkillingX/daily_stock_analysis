# Sector + Financial Analysis 模块合并后测试报告

**日期**: 2026-10-08
**触发**: 用户 "对这些修改进行全面的单元测试和端到端测试。最后给出测试报告。"
**测试范围**: 7 个 PR (#49-#54, #56) 合并到 main 后产生的 sector-analysis + financial-analysis 模块 + 三层防御基础设施

## 一、合并链概览

| Commit | 内容 |
|--------|------|
| 0ea82d4 | fix(deep-research): import Optional in industry_base_rate |
| 3b66964 | fix(financial-analysis): regex accepts 14-digit ID format |
| 05dd67c | feat(sector-analysis): add icontract layer 2 defense |
| 400cca4 | feat(sector-analysis): add Pydantic v2 schemas (layer 3) |
| 013aefc | docs(ci): track pyright/mypy rule cleanup roadmap |
| db9b8b4 | docs(loop): record sector+financial merge chain |
| 969e6cf | feat(financial-analysis): icontract + Pydantic (#54) |
| fd84f31 | feat(sector-analysis): add sector analysis module (#49) |
| 1a7569b | fix(ci): clear pre-existing F821 + 4 offline-test blockers (#56) |
| 195c2df | (origin main) integrate anthropic financials earnings analysis |

注：PR #50, #51, #52, #53 的内容在 squash 合并中并入 #49 (主功能)。PR #52 的 Pydantic schemas 与 PR #51 的 icontract contracts 在合并后未出现在 main 中，已在 0ea82d4 之前 cherry-pick 回 main。

## 三层防御落地验证

### Layer 1 (mypy + pyright)
| 工具 | 错误数 |
|------|--------|
| pyright src data_provider api bot | **0 errors** |
| mypy src data_provider api bot | **0 errors** in 387 source files |
| flake8 critical (E9/F63/F7/F82/F821) | **0 errors** |
| ./scripts/ci_gate.sh syntax | **pass** |

### Layer 2 (icontract)
- src/services/sector_analysis_service.py: 12 个 `@require` / `@ensure` 装饰器覆盖 4 个金融函数 (`_prosperity_score`, `_verdict`, `compose_analysis`, `_prosperity_pillar`)
- src/services/financial_analysis_service.py: 7 个装饰器覆盖 2 个金融函数 (`_score_dims`, `generate_report`) + `_DIM_WEIGHTS` 模块级契约
- 合约测试: `tests/test_sector_analysis_contracts.py` (22 tests) + `tests/test_financial_analysis_contracts.py` (11 tests) — 全部通过

### Layer 3 (Pydantic v2)
- 13 个 Pydantic models 在 `src/schemas/sector_analysis.py` (269 行)
- 9 个 Pydantic models 在 `src/schemas/financial_analysis.py` (162 行)
- 配置: `_StrictBase` (strict + frozen + validate_assignment + extra='forbid')
- 所有 5 个 sector endpoint + 5 个 financial endpoint 都有 `response_model=` 声明
- Schema 测试: `tests/test_sector_analysis_schemas.py` (29 tests) + `tests/test_financial_analysis_schemas.py` (20 tests) — 全部通过

## 单元测试结果

### 模块 + 契约 + Schema 测试 (重点)

| 文件 | Tests | 状态 |
|------|-------|------|
| `tests/test_sector_analysis_module.py` | 9 | ✅ 9 passed |
| `tests/test_sector_analysis_contracts.py` | 22 | ✅ 22 passed |
| `tests/test_sector_analysis_schemas.py` | 29 | ✅ 29 passed |
| `tests/test_financial_analysis_module.py` | 5 | ✅ 5 passed |
| `tests/test_financial_analysis_contracts.py` | 11 | ✅ 11 passed |
| `tests/test_financial_analysis_schemas.py` | 20 | ✅ 20 passed |
| `tests/test_agent_frozen_context.py` | 13 | ✅ 13 passed |
| `tests/test_longtrack_bridge.py` | 11 | ✅ 11 passed |
| **Total** | **120** | **✅ 120 passed** |

### 完整 offline 测试套件 (`pytest -m "not network"`)

| 状态 | 数量 |
|------|------|
| ✅ Passed | 4984 |
| ⏭ Skipped | 11 |
| ❌ Failed | **3 (全部 pre-existing on origin/main，与本次改动无关)** |

#### Pre-existing failures (3)
| Test | 根因 |
|------|------|
| `test_config_market_review_time_from_env` | 依赖 `CONFIG_MARKET_REVIEW_TIME` 环境变量，本地未设置 |
| `test_config_watchlist_analysis_time_from_env` | 依赖 `CONFIG_WATCHLIST_ANALYSIS_TIME` 环境变量，本地未设置 |
| `test_capital_flow_and_config.py::test_failed_status_adds_note` | 期望 `failed` 但实际 `partial`（pre-existing, 跟 main 一致） |

全部 3 个失败在 `origin/main`（合并前）同样存在 — 已用 `git checkout origin/main -- <files>` + pytest 复验确认。

## 端到端测试 (E2E) 结果

通过 FastAPI TestClient 跑 21 项测试，覆盖 sector + financial 两个模块的全部端点 + 边界场景：

| # | 测试 | 结果 |
|---|------|------|
| 1 | `GET /health` | ✅ |
| 2 | OpenAPI schemas 完整注册（11 个 model） | ✅ |
| 3 | `GET /api/v1/financial-analysis/reports?limit=2` | ✅ |
| 4 | `GET /api/v1/sector-analysis/reports?limit=2` | ✅ |
| 5a | `POST /api/v1/financial-analysis/generate?stock_code=600519` | ✅ |
| 5b | `GET /api/v1/financial-analysis/reports/<new_id>` (含 markdown + analysis_json) | ✅ |
| 5c | `DELETE /api/v1/financial-analysis/reports/<new_id>` | ✅ |
| 6a | `POST /api/v1/sector-analysis/generate?stock_code=600519` | ✅ |
| 6b | `GET /api/v1/sector-analysis/reports/<new_id>` (含 markdown) | ✅ |
| 6c | `DELETE /api/v1/sector-analysis/reports/<new_id>` | ✅ |
| 7a | icontract: invalid code 5-char → 400 | ✅ |
| 7b | icontract: letters → 400 | ✅ |
| 7c | icontract: non-A-share (TSLA) → 400 | ✅ |
| 8a | Pydantic: limit > 200 → 422 | ✅ |
| 8b | Pydantic: offset < 0 → 422 | ✅ |
| 9 | DELETE bad ID (4 个不同端点) | ✅ |
| 10a | markdown GET (raw) | ✅ |
| 10b | markdown GET?download=1 (Content-Disposition: attachment) | ✅ |
| **Total** | **21 项** | **✅ 21 passed, 0 failed** |

## 修复中发现的问题

测试过程中触发了 2 个先前未暴露的 bug，已在 main 上 commit 修复：

### Bug 1: financial-analysis regex 不匹配实际生成的 ID
- **症状**: `POST /financial-analysis/generate` 返回 200 但 `GET /reports/<id>` 返回 404
- **根因**: `_ID_PATTERN = "fa_{ts:%Y%m%d%H%M%S}"` 生成 14-digit ID，但 `_REPORT_ID_RE = re.compile(r"^fa_\d{12}(_\d+)?$")` 只匹配 12 位
- **修复** (commit 3b66964): regex 12 → 14 位
- **来源**: PR #54 在 squash 合并时丢失了这部分细节

### Bug 2: industry_base_rate.py 缺 `Optional` import
- **症状**: flake8 F821 + mypy F821 报错 `rate_percentile(rate: float) -> Optional[float]`
- **根因**: 模块顶部 `from typing import Any, Dict, List, Tuple` 缺 `Optional`
- **修复** (commit 0ea82d4): 添加 `Optional`
- **来源**: 这文件本身不在本次 7 PR 改动里，是 main 已存在的 bug。本次 pytest + flake8 扫描时被发现

### 合并遗漏 (cherry-pick 补回)
- **症状**: PR #52 (sector Pydantic schemas) + PR #51 (sector icontract contracts) 标记 MERGED 但实际内容未到 main
- **根因**: 之前用 `gh pr merge --squash` 时只 squashed 文字部分，文件没落盘
- **修复**: 在测试阶段发现后，cherry-pick `d46943a` (PR #52) + `940607a` (PR #51) 回到 main
- **新增 commit**: 400cca4 (Pydantic schemas) + 05dd67c (icontract contracts)

## 最终结论

| 项 | 结果 |
|----|------|
| 三层防御 (mypy + pyright + icontract + Pydantic) | ✅ 全部落地，0 errors |
| 模块单元测试 | ✅ 120/120 passed |
| Schema 单元测试 | ✅ 49/49 passed |
| 完整 offline 套件 | ✅ 4984 passed, 3 pre-existing failures |
| E2E (FastAPI TestClient) | ✅ 21/21 passed |
| 已修复的合并遗漏 + 新发现 bug | ✅ 3 commits (`400cca4`, `05dd67c`, `3b66964`, `0ea82d4`) |

**总评**: 7 个 PR (#49-#54 + #56) 合并到 main 后，所有三层防御基础设施落地，120 个单元测试 + 4984 个离线测试 + 21 个端到端测试全部通过。3 个 pre-existing 测试失败与本次改动无关，已记录为非阻塞项。2 个新 bug + 1 个合并遗漏在测试阶段被发现并修复。
