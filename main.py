"""토스증권 자동매매 프로그램 메인 엔트리포인트.

사용법:
    python main.py              # 웹 대시보드 실행 (기본 포트 8080)
    python main.py --headless   # 헤드리스 모드로 실행
"""

import argparse
import logging
import uvicorn

from src.config import AppConfig
from src.broker.toss import TossBroker
from src.risk import RiskManager, RiskConfig
from src.strategy.multi_confirm import MultiConfirmStrategy
from src.scheduler import TradingScheduler
from src.dashboard.app import app, set_scheduler, set_config
from src.utils.logger import setup_logger

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="토스증권 자동매매")
    parser.add_argument("--headless", action="store_true", help="브라우저 헤드리스 모드")
    parser.add_argument("--port", type=int, default=None, help="대시보드 포트 (기본: 8080)")
    parser.add_argument("--log-level", default="INFO", help="로그 레벨")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_logger(args.log_level)

    config = AppConfig.load()
    if args.headless:
        config.toss.headless = True
    if args.port:
        config.dashboard.port = args.port

    broker = TossBroker(config.toss)
    risk_config = RiskConfig(
        max_risk_per_trade=2.0,   # 1회 최대 총자산 2% 리스크
        stop_loss_pct=1.5,        # 손절: -1.5%
        take_profit_pct=3.0,      # 익절: +3.0%
        trailing_stop_pct=1.0,    # 트레일링 스탑: 최고가 대비 -1.0%
        max_positions=5,          # 동시 최대 5종목
        max_daily_loss_pct=5.0,   # 일일 최대 손실 -5%
        cooldown_after_loss=3,    # 3연속 손실 시 쿨다운
    )
    risk_manager = RiskManager(risk_config)
    strategy = MultiConfirmStrategy(risk_manager)
    scheduler = TradingScheduler(broker, strategy, config.trading, risk_manager)

    set_scheduler(scheduler)
    set_config(config)

    logger.info("대시보드 시작: http://%s:%d", config.dashboard.host, config.dashboard.port)
    logger.info("전략: 다중확인 (RSI+MACD+BB+MA+스토캐스틱+거래량)")
    logger.info("리스크: 손절 %.1f%% / 익절 %.1f%% / 트레일링 %.1f%%",
                risk_config.stop_loss_pct, risk_config.take_profit_pct,
                risk_config.trailing_stop_pct)

    uvicorn.run(app, host=config.dashboard.host, port=config.dashboard.port)


if __name__ == "__main__":
    main()
