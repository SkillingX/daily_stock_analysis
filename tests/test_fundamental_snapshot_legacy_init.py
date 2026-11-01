# -*- coding: utf-8 -*-
"""Issue #71-F：legacy fundamental_snapshot 表启动自愈（as_of_date 列 + 索引）。

部署库建表先于 as_of_date 列引入：create_all 不改既有表，ORM 全列 SELECT
因此 OperationalError、读取 fail-open。本文件在 legacy scratch SQLite 上
真实初始化 DatabaseManager（两次），验证：

- 缺列旧库启动后补齐 nullable as_of_date 列与 ix_fundamental_snapshot_as_of；
- 历史行 payload/created_at 保持原样，未知日期保持 NULL（禁止任何回填）；
- 修复前读取 fail-open 返回 None、修复后能真实读到 payload；
- 新写入带显式日期与不带日期（NULL）两条路径；
- 重复初始化幂等（不重复加列/建索引、不动数据）。

scripts/migrate_fundamental_snapshot_as_of_20260708.py 的 schema 函数仅作
参照，本测试及被测路径绝不执行其 main / migrate_backfill（猜测日期）。
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import tempfile
import unittest
from typing import Any, Tuple

from sqlalchemy import inspect

from src.storage import DatabaseManager

_LEGACY_CREATE_SQL = (
    "CREATE TABLE fundamental_snapshot ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "query_id VARCHAR(64) NOT NULL, "
    "code VARCHAR(10) NOT NULL, "
    "payload TEXT NOT NULL, "
    "source_chain TEXT, "
    "coverage TEXT, "
    "created_at DATETIME)"
)
_LEGACY_INDEX_SQL = (
    "CREATE INDEX ix_fundamental_snapshot_query_code ON fundamental_snapshot (query_id, code)",
    "CREATE INDEX ix_fundamental_snapshot_created ON fundamental_snapshot (created_at)",
)
_HISTORICAL_PAYLOAD = '{"earnings": {"yoy": 12.3}, "growth": {"revenue": 100.0}}'
_HISTORICAL_CREATED_AT = "2026-01-15 10:00:00"


def _build_legacy_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_LEGACY_CREATE_SQL)
        for index_sql in _LEGACY_INDEX_SQL:
            conn.execute(index_sql)
        conn.execute(
            "INSERT INTO fundamental_snapshot (query_id, code, payload, source_chain, coverage, created_at) "
            "VALUES ('q-legacy', '600519', ?, '[]', '{}', ?)",
            (_HISTORICAL_PAYLOAD, _HISTORICAL_CREATED_AT),
        )
        conn.commit()
    finally:
        conn.close()


class FundamentalSnapshotLegacyInitTest(unittest.TestCase):
    """真实 DatabaseManager 初始化（非 mock）：legacy 库启动自愈与数据保全。"""

    def setUp(self) -> None:
        DatabaseManager.reset_instance()
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._temp_dir.name, "legacy_fundamental_snapshot.db")
        _build_legacy_db(self.db_path)

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        self._temp_dir.cleanup()

    def _table_columns(self, db: DatabaseManager) -> dict[str, dict[str, Any]]:
        inspector = inspect(db._engine)
        return {c["name"]: c for c in inspector.get_columns("fundamental_snapshot")}

    def _index_names(self, db: DatabaseManager) -> set[str]:
        return {i["name"] for i in inspect(db._engine).get_indexes("fundamental_snapshot")}

    def _query(self, sql: str) -> Tuple[Any, ...]:
        """scratch 库只读查询；连接显式关闭，不依赖临时句柄析构。"""
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(sql).fetchone()
        assert row is not None
        return tuple(row)

    def test_legacy_init_adds_nullable_column_and_index(self) -> None:
        db = DatabaseManager(db_url=f"sqlite:///{self.db_path}")

        columns = self._table_columns(db)
        self.assertIn("as_of_date", columns)
        self.assertTrue(columns["as_of_date"]["nullable"], "as_of_date 必须保持 nullable")
        self.assertIn("ix_fundamental_snapshot_as_of", self._index_names(db))

    def test_legacy_historical_row_preserved_and_readable(self) -> None:
        db = DatabaseManager(db_url=f"sqlite:///{self.db_path}")

        payload_text, created_at, as_of_date = self._query(
            "SELECT payload, created_at, as_of_date FROM fundamental_snapshot WHERE id = 1"
        )
        self.assertEqual(payload_text, _HISTORICAL_PAYLOAD, "历史 payload 必须逐字节保持")
        self.assertEqual(created_at, _HISTORICAL_CREATED_AT, "历史 created_at 必须保持")
        self.assertIsNone(as_of_date, "未知日期必须保持 NULL，禁止从 payload/created_at 推断回填")

        # 修复前：ORM 全列 SELECT 命中缺列 → fail-open 返回 None。
        # 修复后：能真实读到历史 payload。
        payload = db.get_latest_fundamental_snapshot("q-legacy", "600519")
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload.get("earnings"), {"yoy": 12.3})

    def test_second_initialization_is_idempotent(self) -> None:
        db = DatabaseManager(db_url=f"sqlite:///{self.db_path}")
        self.assertIn("as_of_date", self._table_columns(db))

        DatabaseManager.reset_instance()
        db2 = DatabaseManager(db_url=f"sqlite:///{self.db_path}")

        columns = list(self._table_columns(db2))
        self.assertEqual(columns.count("as_of_date"), 1, "重复初始化不得重复加列")
        index_names = [n for n in self._index_names(db2) if n == "ix_fundamental_snapshot_as_of"]
        self.assertEqual(index_names, ["ix_fundamental_snapshot_as_of"], "重复初始化不得重复建索引")

        count = self._query("SELECT COUNT(*) FROM fundamental_snapshot")[0]
        self.assertEqual(count, 1, "重复初始化不得改动既有数据")
        self.assertIsNotNone(db2.get_latest_fundamental_snapshot("q-legacy", "600519"))

    def test_new_writes_with_explicit_and_null_dates(self) -> None:
        db = DatabaseManager(db_url=f"sqlite:///{self.db_path}")

        saved = db.save_fundamental_snapshot(
            "q-new", "300398", {"main_inflow": 1.0}, as_of_date="2026-10-30"
        )
        self.assertEqual(saved, 1)
        saved_null = db.save_fundamental_snapshot("q-null", "600519", {"main_inflow": 2.0})
        self.assertEqual(saved_null, 1)

        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT query_id, as_of_date FROM fundamental_snapshot ORDER BY id"
            ).fetchall()
        by_query = dict(rows)
        self.assertEqual(by_query["q-new"], "2026-10-30")
        self.assertIsNone(by_query["q-null"], "调用方未提供日期时写入 NULL")

        latest = db.get_latest_fundamental_snapshot("q-new", "300398")
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.get("main_inflow"), 1.0)

    def test_fresh_install_has_column_and_index(self) -> None:
        fresh_path = os.path.join(self._temp_dir.name, "fresh_fundamental_snapshot.db")
        db = DatabaseManager(db_url=f"sqlite:///{fresh_path}")

        self.assertIn("as_of_date", self._table_columns(db))
        self.assertIn("ix_fundamental_snapshot_as_of", self._index_names(db))


if __name__ == "__main__":
    unittest.main()
