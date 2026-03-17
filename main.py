"""토스증권 자동매매 프로그램 메인 엔트리포인트.

사용법:
    python main.py              # 웹 대시보드 실행 (기본 포트 8080)
    python main.py --headless   # 헤드리스 모드로 실행
"""

import argparse
import asyncio
import logging
import uvicorn

from src.config import AppConfig
from src.broker.toss import TossBroker
from src.strategy.base import BaseStrategy, Signal, SignalType
from src.scheduler import TradingScheduler
from src.dashboard.app import app, set_scheduler, set_config
from src.utils.logger import setup_logger

logger = logging.getLogger(__name__)


class PlaceholderStrategy(BaseStrategy):
    """기본 전략 (아무 동작 안 함). 실제 전략 구현 전 테스트용."""

    async def analyze(self, code, broker):
        price = await broker.get_price(code)
        if price:
            logger.info("[%s] %s 현재가: %d원", self.name, price.name, price.current_price)
        return Signal(type=SignalType.HOLD, code=code, reason="전략 미설정")


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
    strategy = PlaceholderStrategy()
    scheduler = TradingScheduler(broker, strategy, config.trading)

    set_scheduler(scheduler)
    set_config(config)

    logger.info("대시보드 시작: http://%s:%d", config.dashboard.host, config.dashboard.port)
    logger.info("브라우저에서 대시보드를 열고 '시작' 버튼을 눌러 매매를 시작하세요.")
    logger.info("토스증권 로그인은 대시보드에서 '시작' 시 자동으로 진행됩니다.")

    uvicorn.run(app, host=config.dashboard.host, port=config.dashboard.port)


if __name__ == "__main__":
    main()
