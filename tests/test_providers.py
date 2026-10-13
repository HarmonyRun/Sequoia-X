"""Tests for market data normalization and automatic failover."""

from types import SimpleNamespace

import pandas as pd

from sequoia_x.data import providers


def test_eastmoney_normalizes_columns_and_volume(monkeypatch) -> None:
    raw = pd.DataFrame(
        [
            {
                "日期": "2026-10-09",
                "开盘": 10,
                "最高": 12,
                "最低": 9,
                "收盘": 11,
                "成交量": 123,
                "成交额": 4567,
            }
        ]
    )
    fake_akshare = SimpleNamespace(stock_zh_a_hist=lambda **_kwargs: raw)
    monkeypatch.setitem(__import__("sys").modules, "akshare", fake_akshare)

    result = providers.AkShareProvider("eastmoney").fetch_history(
        "000001", "2026-10-01", "2026-10-09"
    )

    assert list(result.columns) == providers.NORMALIZED_COLUMNS
    assert result.iloc[0]["volume"] == 12300
    assert result.iloc[0]["turnover"] == 4567


def test_sina_uses_amount_instead_of_turnover_ratio(monkeypatch) -> None:
    raw = pd.DataFrame(
        [
            {
                "date": "2026-10-09",
                "open": 10,
                "high": 12,
                "low": 9,
                "close": 11,
                "volume": 12300,
                "amount": 456700,
                "turnover": 1.23,
            }
        ]
    )
    fake_akshare = SimpleNamespace(stock_zh_a_daily=lambda **_kwargs: raw)
    monkeypatch.setitem(__import__("sys").modules, "akshare", fake_akshare)

    result = providers.AkShareProvider("sina").fetch_history(
        "000001", "2026-10-01", "2026-10-09"
    )

    assert list(result.columns) == providers.NORMALIZED_COLUMNS
    assert result.iloc[0]["turnover"] == 456700


def test_auto_pool_switches_and_continues(monkeypatch) -> None:
    calls: list[str] = []

    class FakeProvider:
        def __init__(self, name: str) -> None:
            self.name = name

        def fetch_history(self, _symbol: str, _start: str, _end: str) -> pd.DataFrame:
            calls.append(self.name)
            if self.name == "eastmoney":
                raise providers.ProviderError("limited")
            return pd.DataFrame(
                [["2026-10-09", 10, 11, 9, 10.5, 1000, 10000]],
                columns=providers.NORMALIZED_COLUMNS,
            )

        def close(self) -> None:
            return

    monkeypatch.setattr(
        providers,
        "create_provider",
        lambda name, _timeout=60: FakeProvider(name),
    )
    pool = providers.ProviderPool("auto")

    _, source, errors = pool.fetch("000001", "2026-10-01", "2026-10-09")
    assert source == "tencent"
    assert errors == ["limited"]
    assert pool.active_name == "tencent"

    pool.fetch("000002", "2026-10-01", "2026-10-09")
    assert calls == ["eastmoney", "eastmoney", "tencent", "tencent"]


def test_auto_pool_retries_transient_failure_before_switching(monkeypatch) -> None:
    calls = 0

    class FlakyProvider:
        name = "eastmoney"

        def fetch_history(self, _symbol: str, _start: str, _end: str) -> pd.DataFrame:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise providers.ProviderError("temporary disconnect")
            return pd.DataFrame(
                [["2026-10-09", 10, 11, 9, 10.5, 1000, 10000]],
                columns=providers.NORMALIZED_COLUMNS,
            )

        def close(self) -> None:
            return

    monkeypatch.setattr(providers, "AUTO_SOURCE_ORDER", ("eastmoney",))
    monkeypatch.setattr(providers, "create_provider", lambda *_args, **_kwargs: FlakyProvider())
    monkeypatch.setattr(providers.time, "sleep", lambda _seconds: None)

    frame, source, errors = providers.ProviderPool("auto").fetch(
        "000001", "2026-10-01", "2026-10-09"
    )

    assert len(frame) == 1
    assert source == "eastmoney"
    assert errors == []
    assert calls == 2
