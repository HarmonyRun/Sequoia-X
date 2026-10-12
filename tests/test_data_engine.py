"""数据引擎属性测试。"""

import sqlite3
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
from hypothesis import given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine


def make_engine_in(tmp_dir: str) -> tuple[DataEngine, Settings]:
    """创建使用临时数据库的 DataEngine 实例。"""
    settings = Settings(
        db_path=str(Path(tmp_dir) / "test.db"),
        start_date="2024-01-01",
        feishu_webhook_url="https://example.com/hook",
    )
    engine = DataEngine(settings)
    return engine, settings


# Property 4: (symbol, date) 唯一约束防止重复写入
@given(
    symbol=st.text(min_size=6, max_size=6, alphabet="0123456789"),
    trade_date=st.dates(min_value=date(2024, 1, 1), max_value=date(2025, 12, 31)),
)
@h_settings(max_examples=50, deadline=None)
def test_unique_symbol_date_constraint(symbol: str, trade_date: date) -> None:
    """相同 (symbol, date) 插入两次，数据库中该组合记录数应保持为 1。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        row = {
            "symbol": symbol, "date": str(trade_date),
            "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
            "volume": 1000.0, "turnover": 10500.0,
        }
        df = pd.DataFrame([row])
        with sqlite3.connect(engine.db_path) as conn:
            df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi")
            try:
                df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi")
            except sqlite3.IntegrityError:
                pass
            count = conn.execute(
                "SELECT COUNT(*) FROM stock_daily WHERE symbol=? AND date=?",
                (symbol, str(trade_date)),
            ).fetchone()[0]
        assert count == 1


def test_provider_change_replaces_full_symbol_history(monkeypatch) -> None:
    """Changing provider must not splice incompatible adjusted-price series."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        with sqlite3.connect(engine.db_path) as conn:
            conn.execute(
                """
                INSERT INTO stock_daily
                    (symbol, date, open, high, low, close, volume, turnover)
                VALUES ('000001', '2024-01-02', 1, 1, 1, 1, 100, 100)
                """
            )
            conn.commit()

        class FakePool:
            active_name = "tencent"

            def __init__(self, *_args, **_kwargs) -> None:
                self.calls: list[tuple[str, str]] = []

            def fetch(self, _symbol: str, start: str, end: str):
                self.calls.append((start, end))
                frame = pd.DataFrame(
                    [
                        {
                            "date": "2024-01-03",
                            "open": 20,
                            "high": 21,
                            "low": 19,
                            "close": 20,
                            "volume": 200,
                            "turnover": 2000,
                        }
                    ]
                )
                return frame, "tencent", []

            def close(self) -> None:
                return

        monkeypatch.setattr("sequoia_x.data.engine.ProviderPool", FakePool)
        engine.data_request_interval = 0
        result = engine.backfill(["000001"])

        assert result["rows"] == 1
        with sqlite3.connect(engine.db_path) as conn:
            rows = conn.execute(
                "SELECT date, close FROM stock_daily WHERE symbol='000001' ORDER BY date"
            ).fetchall()
            source = conn.execute(
                "SELECT source FROM stock_sync_state WHERE symbol='000001'"
            ).fetchone()[0]
        assert rows == [("2024-01-03", 20.0)]
        assert source == "tencent"
