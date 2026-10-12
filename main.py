"""Sequoia-X V2 主程序入口。

三种运行模式：
  python main.py               # 日常模式：8进程增量补数据 + 跑策略 + 飞书推送（2~3分钟）
  python main.py --backfill    # 回填模式：baostock 拉全市场历史K线（首次/补数据用，约12分钟）
  python main.py --update-only # 仅同步当日最新行情，不运行策略
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from sequoia_x.analysis import LlmAnalyzer
from sequoia_x.core.config import get_settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine
from sequoia_x.notify.feishu import FeishuNotifier
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.high_tight_flag import HighTightFlagStrategy
from sequoia_x.strategy.limit_up_shakeout import LimitUpShakeoutStrategy
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.private_placement import PrivatePlacementStrategy
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy
from sequoia_x.strategy.uptrend_limit_down import UptrendLimitDownStrategy

load_dotenv()


def main() -> None:
    parser = argparse.ArgumentParser(description="Sequoia-X V2 选股系统")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="回填模式：通过 baostock 拉取全市场历史 K 线（约12分钟）",
    )
    parser.add_argument(
        "--update-only",
        action="store_true",
        help="仅同步当日最新行情，不运行选股和推送",
    )
    args = parser.parse_args()

    try:
        # 1. 初始化配置
        settings = get_settings()

        # 2. 初始化日志
        logger = get_logger(__name__)
        logger.info("Sequoia-X V2 启动")

        # 3. 初始化数据引擎
        engine = DataEngine(settings)

        if args.backfill:
            # ── 回填模式：单线程保守拉历史 K 线，自动多轮重跑 ──
            logger.info("进入回填模式...")
            all_symbols = engine.get_all_symbols()
            engine.backfill(all_symbols)
            logger.info("Sequoia-X V2 回填模式运行完成")
            return

        # ── 日常模式：单次 API 补今天 + 策略 + 推送 ──
        logger.info("开始拉取最新快照...")
        count = engine.sync_today_bulk()
        logger.info(f"快照同步完成，写入 {count} 只股票")
        if args.update_only:
            logger.info("Sequoia-X V2 当日行情更新完成")
            return

        # 4. 策略列表（新增策略在此追加即可）
        strategies: list[BaseStrategy] = [
            MaVolumeStrategy(engine=engine, settings=settings),
            TurtleTradeStrategy(engine=engine, settings=settings),
            HighTightFlagStrategy(engine=engine, settings=settings),
            LimitUpShakeoutStrategy(engine=engine, settings=settings),
            UptrendLimitDownStrategy(engine=engine, settings=settings),
            RpsBreakoutStrategy(engine=engine, settings=settings),
            PrivatePlacementStrategy(engine=engine, settings=settings),
        ]

        notifier = FeishuNotifier(settings)
        strategy_catalog = {
            "MaVolumeStrategy": (
                "均线放量",
                "5 日均线上穿 20 日均线，同时成交量显著放大。",
            ),
            "TurtleTradeStrategy": (
                "海龟突破",
                "收盘价突破前 20 个交易日高点，并通过成交额和阳线过滤。",
            ),
            "HighTightFlagStrategy": (
                "高而窄旗形",
                "强势上涨后在高位窄幅整理、波动和成交量收敛。",
            ),
            "LimitUpShakeoutStrategy": (
                "涨停洗盘",
                "涨停后出现放量回踩，但关键价格结构尚未破坏。",
            ),
            "UptrendLimitDownStrategy": (
                "上升趋势跌停",
                "长期上升趋势中的异常跌停或急跌，寻找潜在反包机会。",
            ),
            "RpsBreakoutStrategy": (
                "RPS 强势突破",
                "相对强度靠前且价格向上突破，寻找市场领先标的。",
            ),
            "PrivatePlacementStrategy": (
                "定向增发",
                "结合定向增发事件和价格条件筛选潜在重估标的。",
            ),
        }
        result_items: list[dict[str, Any]] = []
        pushed_count = 0

        # 5. 遍历策略，有结果则推送至对应机器人
        for strategy in strategies:
            strategy_name = type(strategy).__name__
            logger.info(f"执行策略：{strategy_name}")

            selected: list[str] = strategy.run()
            logger.info(f"{strategy_name} 选出 {len(selected)} 只股票")
            display_name, description = strategy_catalog.get(
                strategy_name, (strategy_name, "自定义量化选股策略。")
            )
            result_items.append(
                {
                    "strategy": strategy_name,
                    "name": display_name,
                    "description": description,
                    "webhook_key": strategy.webhook_key,
                    "symbols": selected,
                    "count": len(selected),
                }
            )

            if selected and settings.push_strategy_results:
                if notifier.send(
                    symbols=selected,
                    strategy_name=strategy_name,
                    webhook_key=strategy.webhook_key,
                ):
                    pushed_count += 1
            else:
                logger.info(f"{strategy_name} 无结果或已关闭策略推送，跳过")

        unique_symbols = sorted({symbol for item in result_items for symbol in item["symbols"]})
        ai_analysis: str | None = None
        ai_error: str | None = None
        ai_pushed = False
        ai_skipped_reason: str | None = None
        if settings.llm_enabled and unique_symbols:
            try:
                logger.info("开始调用大模型进行二次分析")
                ai_analysis = LlmAnalyzer(settings).analyze(result_items)
                if settings.push_ai_analysis:
                    ai_pushed = notifier.send_analysis(
                        ai_analysis,
                        f"共 {len(unique_symbols)} 只去重标的，来自 {len(result_items)} 个策略。",
                    )
            except Exception as exc:
                ai_error = str(exc)
                logger.exception("大模型分析失败，选股结果仍会保存")
        elif settings.llm_enabled:
            ai_skipped_reason = "本次所有策略均无选股结果，已跳过大模型分析和飞书推送"
            logger.info(ai_skipped_reason)

        result = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "trade_date": datetime.now().date().isoformat(),
            "data_source": settings.data_source,
            "snapshot_count": count,
            "strategies": result_items,
            "unique_symbols": unique_symbols,
            "total_unique": len(unique_symbols),
            "strategy_push_count": pushed_count,
            "ai": {
                "enabled": settings.llm_enabled,
                "model": settings.llm_model,
                "analysis": ai_analysis,
                "error": ai_error,
                "pushed": ai_pushed,
                "skipped_reason": ai_skipped_reason,
            },
        }
        result_path = Path(settings.result_path)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = result_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(result_path)
        history_dir = result_path.parent / "history"
        history_dir.mkdir(exist_ok=True)
        history_name = datetime.now().strftime("%Y%m%d-%H%M%S.json")
        (history_dir / history_name).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    except Exception:
        try:
            _logger = get_logger(__name__)
            _logger.exception("主流程发生未捕获异常，程序终止")
        except Exception:
            import traceback
            traceback.print_exc()
        sys.exit(1)

    logger.info("Sequoia-X V2 运行完成")


if __name__ == "__main__":
    main()
