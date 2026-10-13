"""数据引擎模块：负责 SQLite 行情数据存储与 baostock 增量同步。"""

import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.providers import AUTO_SOURCE_ORDER, ProviderError, ProviderPool

logger = get_logger(__name__)


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stock_daily (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol   TEXT    NOT NULL,
    date     TEXT    NOT NULL,
    open     REAL,
    high     REAL,
    low      REAL,
    close    REAL,
    volume   REAL,
    turnover REAL,
    UNIQUE (symbol, date)
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_symbol_date ON stock_daily (symbol, date);
"""

_CREATE_SYNC_STATE_SQL = """
CREATE TABLE IF NOT EXISTS stock_sync_state (
    symbol     TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class DataEngine:
    """行情数据引擎，负责 SQLite 存储和 baostock 数据同步。"""

    def __init__(self, settings: Settings) -> None:
        self.db_path: str = settings.db_path
        self.start_date: str = settings.start_date
        self.data_source: str = settings.data_source.lower()
        self.data_timeout: float = settings.data_timeout
        self.data_request_interval: float = settings.data_request_interval
        self._init_db()

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            conn.execute(_CREATE_SYNC_STATE_SQL)
            conn.commit()
        logger.info(f"数据库初始化完成：{self.db_path}")

    def _get_last_date(self, symbol: str) -> str | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT MAX(date) FROM stock_daily WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        return row[0] if row and row[0] else None

    def get_ohlcv(self, symbol: str) -> pd.DataFrame:
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql(
                "SELECT * FROM stock_daily WHERE symbol = ? ORDER BY date",
                conn,
                params=(symbol,),
            )
        return df

    def _get_symbol_source(self, symbol: str) -> str | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT source FROM stock_sync_state WHERE symbol = ?", (symbol,)
            ).fetchone()
        return row[0] if row else None

    @staticmethod
    def _to_baostock_code(symbol: str) -> str:
        """将纯数字代码转为 baostock 格式：6/9开头 -> sh，其余 -> sz。"""
        prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
        return f"{prefix}.{symbol}"

    # ── 数据同步 ──

    def sync_today_bulk(self) -> int:
        """Fill every local symbol through today using the selected provider policy."""
        symbols = self.get_local_symbols()
        if not symbols:
            logger.warning("本地无股票数据，请先执行历史回填")
            return 0
        result = self.backfill(symbols, incremental=True)
        if result["failed"] and not result["success"] and not result["skipped"]:
            raise RuntimeError("所有行情数据源均不可用，最新数据回填失败")
        return result["rows"]

    def backfill(self, symbols: list[str], *, incremental: bool = False) -> dict[str, int]:
        """Backfill symbols, automatically continuing with another provider on failures."""
        if not symbols:
            raise RuntimeError("未获取到股票列表，无法开始回填")
        today_str = date.today().strftime("%Y-%m-%d")
        pool = ProviderPool(self.data_source, self.data_timeout)
        success = skipped = failed = written = 0
        logger.info(
            "数据源模式: %s；当前数据源: %s",
            self.data_source,
            pool.active_name,
        )
        try:
            for index, symbol in enumerate(symbols, start=1):
                last_date = self._get_last_date(symbol)
                if last_date and last_date >= today_str:
                    skipped += 1
                    continue
                start = last_date or self.start_date
                if last_date:
                    start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime(
                        "%Y-%m-%d"
                    )
                try:
                    frame, source, errors = pool.fetch(symbol, start, today_str)
                    for error in errors:
                        if error.endswith(": empty response"):
                            logger.info("[%s] 当前数据源无该区间数据，继续尝试: %s", symbol, error)
                        else:
                            logger.warning("[%s] 数据源请求失败，自动切换: %s", symbol, error)
                    previous_source = self._get_symbol_source(symbol)
                    replace_history = bool(last_date and previous_source != source)
                    if replace_history and not frame.empty:
                        logger.info(
                            "[%s] 数据源由 %s 切换为 %s，重新拉取完整区间以保持复权一致",
                            symbol,
                            previous_source or "未知",
                            source,
                        )
                        frame, source, additional_errors = pool.fetch(
                            symbol, self.start_date, today_str
                        )
                        for error in additional_errors:
                            if error.endswith(": empty response"):
                                logger.info("[%s] 完整区间当前源无数据: %s", symbol, error)
                            else:
                                logger.warning("[%s] 完整区间数据源请求失败: %s", symbol, error)
                    if frame.empty:
                        skipped += 1
                    else:
                        rows = [
                            (symbol, *row)
                            for row in frame[
                                ["date", "open", "high", "low", "close", "volume", "turnover"]
                            ].itertuples(index=False, name=None)
                        ]
                        with sqlite3.connect(self.db_path) as conn:
                            if replace_history:
                                conn.execute("DELETE FROM stock_daily WHERE symbol = ?", (symbol,))
                            conn.executemany(
                                """
                                INSERT INTO stock_daily
                                    (symbol, date, open, high, low, close, volume, turnover)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                ON CONFLICT(symbol, date) DO UPDATE SET
                                    open=excluded.open, high=excluded.high, low=excluded.low,
                                    close=excluded.close, volume=excluded.volume,
                                    turnover=excluded.turnover
                                """,
                                rows,
                            )
                            conn.execute(
                                """
                                INSERT INTO stock_sync_state (symbol, source, updated_at)
                                VALUES (?, ?, CURRENT_TIMESTAMP)
                                ON CONFLICT(symbol) DO UPDATE SET
                                    source=excluded.source,
                                    updated_at=CURRENT_TIMESTAMP
                                """,
                                (symbol, source),
                            )
                            conn.commit()
                        success += 1
                        written += len(rows)
                        logger.debug("[%s] %s 写入 %d 条", symbol, source, len(rows))
                except ProviderError as exc:
                    failed += 1
                    logger.warning("[%s] 所有可用数据源均失败: %s", symbol, exc)
                if index % (200 if incremental else 100) == 0:
                    logger.info(
                        "已处理 %d/%d，成功 %d 跳过 %d 失败 %d，当前源 %s",
                        index,
                        len(symbols),
                        success,
                        skipped,
                        failed,
                        pool.active_name,
                    )
                if self.data_request_interval > 0:
                    time.sleep(self.data_request_interval)
        finally:
            pool.close()
        logger.info(
            "回填完成 — 写入: %d | 成功: %d | 跳过: %d | 失败: %d",
            written,
            success,
            skipped,
            failed,
        )
        return {"rows": written, "success": success, "skipped": skipped, "failed": failed}

    # ── 股票列表 ──

    def get_all_symbols(self) -> list[str]:
        """Discover the A-share universe with independent source fallbacks."""
        import akshare as ak

        errors: list[str] = []
        order = (self.data_source,) if self.data_source != "auto" else AUTO_SOURCE_ORDER
        # Tencent has no whole-market symbol-list endpoint; use independent list sources.
        list_sources = list(dict.fromkeys((*order, "eastmoney", "sina", "baostock")))
        for source in list_sources:
            try:
                if source in {"eastmoney", "tencent"}:
                    frame = ak.stock_info_a_code_name()
                    column = "code" if "code" in frame.columns else "代码"
                    symbols = frame[column].astype(str).str.zfill(6).tolist()
                elif source == "sina":
                    frame = ak.stock_zh_a_spot()
                    column = "代码" if "代码" in frame.columns else "code"
                    symbols = frame[column].astype(str).str[-6:].tolist()
                else:
                    import socket

                    import baostock as bs

                    socket.setdefaulttimeout(self.data_timeout)
                    login = bs.login()
                    if login.error_code != "0":
                        raise RuntimeError(login.error_msg)
                    try:
                        result = bs.query_stock_basic(code_name="", code="")
                        symbols = []
                        while result.next():
                            row = result.get_row_data()
                            if row[4] == "1" and row[5] == "1":
                                symbols.append(row[0].split(".")[1])
                    finally:
                        bs.logout()
                symbols = sorted(
                    {
                        symbol
                        for symbol in symbols
                        if len(symbol) == 6 and symbol.isdigit()
                    }
                )
                if symbols:
                    logger.info("通过 %s 获取股票列表，共 %d 只", source, len(symbols))
                    return symbols
            except Exception as exc:
                errors.append(f"{source}: {exc}")
                logger.warning("股票列表源 %s 不可用: %s", source, exc)
        local = self.get_local_symbols()
        if local:
            logger.warning("远程股票列表不可用，使用本地缓存的 %d 只股票", len(local))
            return local
        raise RuntimeError("无法获取股票列表: " + " | ".join(errors))

    def get_local_symbols(self) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM stock_daily"
            ).fetchall()
        return [row[0] for row in rows]
