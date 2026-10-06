# -*- coding: utf-8 -*-
"""板块分析模块 icontract 契约测试 (Layer 2 三层防御)。

注意：契约测试必须开 ICONTRACT_SLOW=true 跑（CI 已在 type-safety.yml 配置）：
- 违反契约时 icontract.ViolationError 应被抛出
- 满足契约时正常返回
- 离线运行，不依赖网络

契约来源：src/services/sector_analysis_service.py 中的 @require / @ensure 装饰器
- _prosperity_score: 4 dim 必须 None 或数字；结果 None 或 [0, 100]
- _verdict: 输入 None 或 [0, 100]；输出 (band, β) 都是非空字符串
- compose_analysis: code 6 位数字；返回字典含必需键；weights 和 = 1.0；sector_score None 或 [0, 100]
- _prosperity_pillar: 4 维景气分 None 或 [0, 100]；rank_of ∈ [1, total_boards]
"""

from __future__ import annotations

import pytest
from icontract import ViolationError

from src.services import sector_analysis_service as svc


# ========================================================================
# _prosperity_score 契约
# ========================================================================

class TestProsperityScoreContracts:
    """_prosperity_score 4 维景气均值合约。"""

    def test_valid_input_returns_score_in_range(self):
        result = svc._prosperity_score(
            {"momentum": 80.0, "activity": 60.0, "capital": 40.0, "breadth": 100.0}
        )
        # 70 = (80+60+40+100)/4
        assert result == 70.0
        assert 0.0 <= result <= 100.0

    def test_partial_dims_only_uses_numeric(self):
        result = svc._prosperity_score(
            {"momentum": 80.0, "activity": None, "capital": None, "breadth": 100.0}
        )
        # 90 = (80+100)/2
        assert result == 90.0

    def test_all_missing_returns_none(self):
        assert svc._prosperity_score({"momentum": None, "activity": None}) is None

    def test_out_of_range_dim_raises_contract(self):
        """单 dim=150：均值=150，越界 [0, 100]，应抛 ViolationError。"""
        with pytest.raises(ViolationError):
            svc._prosperity_score({"momentum": 150.0, "activity": None, "capital": None, "breadth": None})

    def test_negative_dim_raises_contract(self):
        with pytest.raises(ViolationError):
            svc._prosperity_score({"momentum": -10.0, "activity": None, "capital": None, "breadth": None})

    def test_non_numeric_dim_raises_contract(self):
        with pytest.raises(ViolationError):
            svc._prosperity_score({"momentum": "high", "activity": 50.0})

    def test_non_dict_input_raises_contract(self):
        with pytest.raises(ViolationError):
            svc._prosperity_score("not a dict")  # type: ignore[arg-type]


# ========================================================================
# _verdict 契约
# ========================================================================

class TestVerdictContracts:
    """_verdict 分档 + β 含义合约。"""

    def test_score_none_returns_insufficient_data(self):
        band, beta = svc._verdict(None)
        assert band == "数据不足"
        assert isinstance(beta, str) and beta  # 非空字符串

    def test_score_75_plus_is_顺风(self):
        band, beta = svc._verdict(80)
        assert band == "顺风"
        assert isinstance(beta, str) and beta

    def test_score_65_to_75_is_偏顺风(self):
        band, beta = svc._verdict(70)
        assert band == "偏顺风"
        assert isinstance(beta, str) and beta

    def test_score_50_to_65_is_中性(self):
        band, beta = svc._verdict(55)
        assert band == "中性"
        assert isinstance(beta, str) and beta

    def test_score_below_50_is_逆风(self):
        band, beta = svc._verdict(30)
        assert band == "逆风"
        assert isinstance(beta, str) and beta

    def test_score_above_100_raises_contract(self):
        with pytest.raises(ViolationError):
            svc._verdict(150)

    def test_score_negative_raises_contract(self):
        with pytest.raises(ViolationError):
            svc._verdict(-1)


# ========================================================================
# compose_analysis 契约
# ========================================================================

class TestComposeAnalysisContracts:
    """compose_analysis 三支柱编排合约。"""

    @pytest.fixture(autouse=True)
    def _mock_pillars(self, monkeypatch):
        """三支柱 mock，保证契约测试离线可跑。"""
        monkeypatch.setattr(
            svc, "_identify_sector",
            lambda code: {
                "industry_em": "半导体",
                "industry_cninfo": "",
                "business_scope": "",
                "sw_chain": {"l1": "", "l2": "", "l3": "", "l3_code": "", "gaps": []},
                "sector_name": "半导体",
                "gaps": [],
            },
        )
        monkeypatch.setattr(
            svc, "_policy_pillar",
            lambda text: {"lean": "supportive", "label": "支持", "score": 66.0, "gaps": []},
        )
        monkeypatch.setattr(
            svc, "_base_rate_pillar",
            lambda text: {
                "base_rate": 0.5, "basis": "test", "hit_keyword": "半导体",
                "is_default": False, "table_version": "v1.0", "table_updated": "2026-09-29",
                "table_industry_count": 21, "percentile": 0.5, "all_rates": [],
            },
        )
        monkeypatch.setattr(
            svc, "_prosperity_pillar",
            lambda name: {
                "sector_found": True,
                "momentum": 80.0, "activity": 60.0, "capital": 40.0, "breadth": 100.0,
                "rank_of": 1, "total_boards": 86,
                "row": {}, "gaps": [],
            },
        )

    def test_returns_full_contract_keys(self):
        result = svc.compose_analysis("600519", "测试股票")
        required = {"identification", "policy", "base_rate", "prosperity",
                    "weights", "gaps", "sector_score", "band"}
        assert required.issubset(result.keys())

    def test_weights_sum_to_one(self):
        result = svc.compose_analysis("600519", "测试股票")
        s = sum(result["weights"].values())
        assert abs(s - 1.0) < 1e-6

    def test_sector_score_in_valid_range(self):
        result = svc.compose_analysis("600519", "测试股票")
        assert result["sector_score"] is not None
        assert 0.0 <= result["sector_score"] <= 100.0

    def test_invalid_code_raises_contract(self):
        with pytest.raises(ViolationError):
            svc.compose_analysis("12345", "测试")  # 5 位

    def test_non_digit_code_raises_contract(self):
        with pytest.raises(ViolationError):
            svc.compose_analysis("ABC123", "测试")  # 含字母

    def test_too_long_code_raises_contract(self):
        with pytest.raises(ViolationError):
            svc.compose_analysis("1234567", "测试")  # 7 位


# ========================================================================
# _prosperity_pillar 契约（mock 网络层后）
# ========================================================================

class TestProsperityPillarContracts:
    """_prosperity_pillar 景气计算合约（rank_of ∈ [1, total_boards] + 4 维 [0, 100]）。"""

    def test_rank_within_total_boards(self, monkeypatch):
        """rank_of ∈ [1, total_boards] 守门通过。"""
        # 86 行 em 表（半导体的涨跌幅在第 50 位 -> rank=37）
        import pandas as pd
        n = 86
        df = pd.DataFrame({
            "板块名称": [f"板块{i}" for i in range(n)],
            "涨跌幅": [-(i + 1) * 0.1 for i in range(n)],  # 降序：前3 板块最高
            "换手率": [3.0] * n,
            "成交额": [1e9] * n,
            "总市值": [None] * n,
            "上涨家数": [10] * n,
            "下跌家数": [20] * n,
            "领涨股票": ["x"] * n,
            "领涨股票-涨跌幅": [None] * n,
            "总成交量": [None] * n,
            "公司家数": [None] * n,
        })
        # 把"半导体"插入第 37 行（前 36 涨幅更高，rank=37）
        df.loc[36, "板块名称"] = "半导体"
        df.loc[36, "涨跌幅"] = -3.7  # 第 37 高
        import akshare as ak
        monkeypatch.setattr(ak, "stock_board_industry_name_em", lambda: df)

        result = svc._prosperity_pillar("半导体")
        assert result["sector_found"] is True
        assert result["total_boards"] == n
        assert result["rank_of"] is not None
        # 守门合约：rank_of ∈ [1, total_boards]
        assert 1 <= result["rank_of"] <= result["total_boards"]

    def test_rank_out_of_range_raises_contract(self, monkeypatch):
        """rank_of > total_boards: 不应出现（验证正常路径不产生越界）。"""
        import pandas as pd
        n = 5
        df = pd.DataFrame({
            "板块名称": ["A", "半导体", "C", "D", "E"],
            "涨跌幅": [1.0, 2.0, 3.0, 4.0, 5.0],
            "换手率": [None] * n,
            "成交额": [None] * n,
            "上涨家数": [None] * n,
            "下跌家数": [None] * n,
        })
        import akshare as ak
        monkeypatch.setattr(ak, "stock_board_industry_name_em", lambda: df)

        result = svc._prosperity_pillar("半导体")
        # 守门合约保证 rank_of ≤ total_boards
        assert result["rank_of"] is not None
        assert result["rank_of"] <= result["total_boards"]
