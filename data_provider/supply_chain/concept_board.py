# -*- coding: utf-8 -*-
"""
Concept Board Data Provider.

Fetches concept board (概念板块) data using akshare.
"""

import logging
from types import ModuleType
from typing import Dict, Any, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class ConceptBoardProvider:
    """
    Provider for concept board (板块概念) data.

    Uses akshare to fetch:
    - Concept board list with price changes
    - Concept board constituents (成分股)
    - Industry board data
    """

    def __init__(self):
        self._ak: Optional[ModuleType] = None
        self._init_akshare()

    def _init_akshare(self):
        """Lazy initialize akshare"""
        if self._ak is None:
            try:
                import akshare as ak

                self._ak = ak
                logger.info("[ConceptBoardProvider] akshare initialized")
            except ImportError:
                logger.warning("[ConceptBoardProvider] akshare not installed")
                self._ak = None

    def get_concept_boards(self) -> List[Dict[str, Any]]:
        """
        Get concept board list with price changes.

        Returns:
            List of concept boards with code, name, change_pct, etc.
        """
        if self._ak is None:
            self._init_akshare()
            if self._ak is None:
                return self._get_mock_concept_boards()

        try:
            df = self._ak.stock_board_concept_name_em()

            boards = []
            for _, row in df.iterrows():
                boards.append(
                    {
                        "code": str(row.get("板块代码", "")),
                        "name": str(row.get("板块名称", "")),
                        "change_pct": float(row.get("涨跌幅", 0)),
                        "turnover": float(row.get("成交额", 0)),
                        "lead_stocks": str(row.get("领涨股票", "")),
                        "updated_at": datetime.now().isoformat(),
                    }
                )

            logger.info(f"[ConceptBoardProvider] Fetched {len(boards)} concept boards")
            return boards

        except Exception as e:
            logger.error(f"[ConceptBoardProvider] Failed to fetch concept boards: {e}")
            return self._get_mock_concept_boards()

    def get_concept_constituents(self, concept_code: str) -> List[str]:
        """
        Get constituent stocks for a concept board.

        Args:
            concept_code: Concept board code

        Returns:
            List of stock codes in the concept
        """
        if self._ak is None:
            self._init_akshare()
            if self._ak is None:
                return []

        try:
            df = self._ak.stock_board_concept_cons_em(symbol=concept_code)

            stocks = []
            for _, row in df.iterrows():
                code = str(row.get("代码", ""))
                if code:
                    stocks.append(code)

            logger.info(
                f"[ConceptBoardProvider] Fetched {len(stocks)} constituents for {concept_code}"
            )
            return stocks

        except Exception as e:
            logger.error(f"[ConceptBoardProvider] Failed to fetch constituents: {e}")
            return []

    def get_stock_concepts(self, stock_code: str) -> List[Dict[str, Any]]:
        """
        Get all concept boards a stock belongs to.

        Args:
            stock_code: Stock code (e.g., 600519)

        Returns:
            List of concept boards with details
        """
        if self._ak is None:
            self._init_akshare()
            if self._ak is None:
                return []

        try:
            df = self._ak.stock_board_industry_cons_em(symbol=stock_code)

            concepts = []
            for _, row in df.iterrows():
                concepts.append(
                    {
                        "code": str(row.get("板块代码", "")),
                        "name": str(row.get("板块名称", "")),
                        "change_pct": float(row.get("涨跌幅", 0)),
                    }
                )

            logger.info(
                f"[ConceptBoardProvider] Stock {stock_code} belongs to {len(concepts)} concepts"
            )
            return concepts

        except Exception as e:
            logger.error(f"[ConceptBoardProvider] Failed to fetch stock concepts: {e}")
            return []

    def get_hot_concepts(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Get hot/rising concept boards.

        Args:
            limit: Maximum number of concepts to return

        Returns:
            List of hot concept boards sorted by change_pct
        """
        boards = self.get_concept_boards()

        if not boards:
            return []

        hot_boards = [b for b in boards if b.get("change_pct", 0) > 0]
        hot_boards.sort(key=lambda x: x.get("change_pct", 0), reverse=True)

        return hot_boards[:limit]

    def get_industry_boards(self) -> List[Dict[str, Any]]:
        """
        Get industry board list.

        Returns:
            List of industry boards
        """
        if self._ak is None:
            self._init_akshare()
            if self._ak is None:
                return []

        try:
            df = self._ak.stock_board_industry_name_em()

            boards = []
            for _, row in df.iterrows():
                boards.append(
                    {
                        "code": str(row.get("板块代码", "")),
                        "name": str(row.get("板块名称", "")),
                        "updated_at": datetime.now().isoformat(),
                    }
                )

            logger.info(f"[ConceptBoardProvider] Fetched {len(boards)} industry boards")
            return boards

        except Exception as e:
            logger.error(f"[ConceptBoardProvider] Failed to fetch industry boards: {e}")
            return []

    def _get_mock_concept_boards(self) -> List[Dict[str, Any]]:
        """Return mock data when akshare is unavailable"""
        return [
            {
                "code": "BK0001",
                "name": "人工智能",
                "change_pct": 3.5,
                "lead_stocks": "科大讯飞,海康威视",
            },
            {
                "code": "BK0002",
                "name": "新能源汽车",
                "change_pct": 2.8,
                "lead_stocks": "比亚迪,宁德时代",
            },
            {
                "code": "BK0003",
                "name": "半导体",
                "change_pct": -1.2,
                "lead_stocks": "中芯国际,韦尔股份",
            },
            {
                "code": "BK0004",
                "name": "白酒",
                "change_pct": 1.5,
                "lead_stocks": "贵州茅台,五粮液",
            },
            {
                "code": "BK0005",
                "name": "医疗器械",
                "change_pct": 0.8,
                "lead_stocks": "迈瑞医疗,药明康德",
            },
        ]


_concept_board_provider: Optional[ConceptBoardProvider] = None


def get_concept_board_provider() -> ConceptBoardProvider:
    """Get singleton provider"""
    global _concept_board_provider
    if _concept_board_provider is None:
        _concept_board_provider = ConceptBoardProvider()
    return _concept_board_provider
