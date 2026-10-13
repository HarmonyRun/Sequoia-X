"""Historical market-data providers with normalization and failover."""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from typing import Protocol

import pandas as pd

SOURCE_NAMES = ("auto", "eastmoney", "tencent", "sina", "baostock")
AUTO_SOURCE_ORDER = ("eastmoney", "tencent", "sina", "baostock")
NORMALIZED_COLUMNS = ["date", "open", "high", "low", "close", "volume", "turnover"]


class ProviderError(RuntimeError):
    """Raised when a market-data provider cannot complete a request."""


class HistoryProvider(Protocol):
    name: str

    def fetch_history(self, symbol: str, start: str, end: str) -> pd.DataFrame: ...

    def close(self) -> None: ...


def market_symbol(symbol: str) -> str:
    """Return an exchange-prefixed symbol understood by Tencent and Sina."""
    if symbol.startswith(("4", "8")):
        return f"bj{symbol}"
    if symbol.startswith(("6", "9")):
        return f"sh{symbol}"
    return f"sz{symbol}"


def _finish_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=NORMALIZED_COLUMNS)
    result = frame[NORMALIZED_COLUMNS].copy()
    result["date"] = pd.to_datetime(result["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for column in NORMALIZED_COLUMNS[1:]:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    result = result.dropna(subset=["date", "close"])
    result = result[result["volume"].fillna(0) > 0]
    result["turnover"] = result["turnover"].fillna(0)
    return result.reset_index(drop=True)


@dataclass
class AkShareProvider:
    name: str
    timeout: float = 60.0

    def fetch_history(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        import akshare as ak

        start_text = start.replace("-", "")
        end_text = end.replace("-", "")
        try:
            if self.name == "eastmoney":
                raw = ak.stock_zh_a_hist(
                    symbol=symbol,
                    period="daily",
                    start_date=start_text,
                    end_date=end_text,
                    adjust="hfq",
                    timeout=self.timeout,
                )
                if raw.empty:
                    return _finish_frame(raw)
                frame = raw.rename(
                    columns={
                        "日期": "date",
                        "开盘": "open",
                        "最高": "high",
                        "最低": "low",
                        "收盘": "close",
                        "成交量": "volume",
                        "成交额": "turnover",
                    }
                )
                # Eastmoney reports A-share volume in lots (100 shares).
                frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce") * 100
            elif self.name == "tencent":
                raw = ak.stock_zh_a_hist_tx(
                    symbol=market_symbol(symbol),
                    start_date=start_text,
                    end_date=end_text,
                    adjust="hfq",
                    timeout=self.timeout,
                )
                if raw.empty:
                    return _finish_frame(raw)
                frame = raw.rename(columns={"amount": "volume"})
                # Tencent's daily endpoint exposes volume in lots but no turnover amount.
                frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce") * 100
                frame["turnover"] = 0.0
            elif self.name == "sina":
                raw = ak.stock_zh_a_daily(
                    symbol=market_symbol(symbol),
                    start_date=start_text,
                    end_date=end_text,
                    adjust="hfq",
                )
                if raw.empty:
                    return _finish_frame(raw)
                # Sina currently returns both ``amount`` (成交额) and ``turnover``
                # (换手率). Renaming amount would create duplicate turnover columns,
                # which makes pandas.to_numeric receive a DataFrame and fail.
                frame = raw.copy()
                frame["turnover"] = raw["amount"]
            else:
                raise ProviderError(f"unsupported AkShare provider: {self.name}")
        except Exception as exc:
            raise ProviderError(f"{self.name}: {exc}") from exc
        return _finish_frame(frame)

    def close(self) -> None:
        return


class BaostockProvider:
    name = "baostock"

    def __init__(self, timeout: float = 60.0) -> None:
        self.timeout = timeout
        self._logged_in = False

    @staticmethod
    def _code(symbol: str) -> str:
        prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
        return f"{prefix}.{symbol}"

    def _login(self) -> None:
        if self._logged_in:
            return
        import baostock as bs

        socket.setdefaulttimeout(self.timeout)
        errors: list[str] = []
        for attempt in range(3):
            result = bs.login()
            if result.error_code == "0":
                self._logged_in = True
                return
            errors.append(result.error_msg)
            time.sleep(2 ** attempt)
        raise ProviderError(f"baostock login failed: {'; '.join(errors)}")

    def fetch_history(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        import baostock as bs

        self._login()
        try:
            result = bs.query_history_k_data_plus(
                self._code(symbol),
                "date,open,high,low,close,volume,amount",
                start_date=start,
                end_date=end,
                frequency="d",
                adjustflag="1",
            )
            if result.error_code != "0":
                raise ProviderError(f"baostock: {result.error_msg}")
            rows: list[list[str]] = []
            while result.next():
                rows.append(result.get_row_data())
            frame = pd.DataFrame(rows, columns=result.fields)
            if frame.empty:
                return _finish_frame(frame)
            frame = frame.rename(columns={"amount": "turnover"})
            return _finish_frame(frame)
        except ProviderError:
            raise
        except Exception as exc:
            self._logged_in = False
            raise ProviderError(f"baostock: {exc}") from exc

    def close(self) -> None:
        if not self._logged_in:
            return
        try:
            import baostock as bs

            bs.logout()
        finally:
            self._logged_in = False


def create_provider(name: str, timeout: float = 60.0) -> HistoryProvider:
    if name == "baostock":
        return BaostockProvider(timeout)
    if name in {"eastmoney", "tencent", "sina"}:
        return AkShareProvider(name, timeout)
    raise ValueError(f"unknown data source: {name}")


class ProviderPool:
    """Keep the active provider and fail over without restarting a backfill."""

    def __init__(self, source: str, timeout: float = 60.0) -> None:
        if source not in SOURCE_NAMES:
            raise ValueError(f"unknown data source: {source}")
        names = AUTO_SOURCE_ORDER if source == "auto" else (source,)
        self.automatic = source == "auto"
        self.providers = [create_provider(name, timeout) for name in names]
        self.index = 0

    @property
    def active_name(self) -> str:
        return self.providers[self.index].name

    def fetch(self, symbol: str, start: str, end: str) -> tuple[pd.DataFrame, str, list[str]]:
        errors: list[str] = []
        provider_attempts = len(self.providers) if self.automatic else 1
        retries_per_provider = 2 if self.automatic else 3
        for attempt in range(provider_attempts):
            provider = self.providers[self.index]
            last_error: ProviderError | None = None
            for retry in range(retries_per_provider):
                try:
                    frame = provider.fetch_history(symbol, start, end)
                    if frame.empty and self.automatic and attempt < provider_attempts - 1:
                        errors.append(f"{provider.name}: empty response")
                        break
                    return frame, provider.name, errors
                except ProviderError as exc:
                    last_error = exc
                    provider.close()
                    if retry < retries_per_provider - 1:
                        time.sleep(1 + retry)
            if last_error is not None:
                errors.append(str(last_error))
            if self.automatic:
                self.index = (self.index + 1) % len(self.providers)
        raise ProviderError(" | ".join(errors))

    def close(self) -> None:
        for provider in self.providers:
            provider.close()
