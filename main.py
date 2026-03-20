"""토스증권 자동매매 프로그램 메인 엔트리포인트.

사용법:
    python main.py               # 웹 대시보드만 실행 (시작 버튼으로 봇 가동)
    python main.py --autostart   # 서버 시작과 동시에 자동 로그인 및 매매 시작
    python main.py --headless    # 헤드리스 모드 (창 없이 백그라운드 실행)
"""

import argparse
import asyncio
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
    parser.add_argument("--autostart", action="store_true",
                        help="서버 시작과 동시에 자동 로그인 및 매매 시작")
    parser.add_argument("--headless", action="store_true", help="브라우저 헤드리스 모드")
    parser.add_argument("--port", type=int, default=None, help="대시보드 포트 (기본: 8080)")
    parser.add_argument("--log-level", default="INFO", help="로그 레벨")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> None:
    setup_logger(args.log_level)

    config = AppConfig.load()
    if args.headless:
        config.toss.headless = True
    if args.port:
        config.dashboard.port = args.port

    broker = TossBroker(config.toss)
    risk_config = RiskConfig(
        max_risk_per_trade=1.0,       # 1회 매매 최대 리스크 1%
        min_rr_ratio=2.0,             # 손익비 최소 1:2
        max_positions=5,
        max_daily_loss_pct=2.0,       # 데일리 컷 -2%
        cooldown_after_loss=3,        # 3-스트라이크
        max_position_pct=25.0,        # 종목당 최대 25%
        min_stop_distance_pct=1.5,    # 최소 손절 거리 1.5% (0.x% 노이즈 손절 방지)
        stop_confirm_ticks=2,         # 손절 2틱 연속 확인
    )
    risk_manager = RiskManager(risk_config, total_budget=config.trading.total_budget)
    strategy = MultiConfirmStrategy(risk_manager)
    scheduler = TradingScheduler(broker, strategy, config.trading, risk_manager)

    set_scheduler(scheduler)
    set_config(config)

    logger.info("대시보드 시작: http://%s:%d", config.dashboard.host, config.dashboard.port)
    logger.info("전략: 미주 단타 SOP (PMH돌파 + EMA추세 / VWAP+RVOL 필터)")
    logger.info("리스크: 1%%/trade, R:R %.0f:1+, 데일리컷 -%.0f%%, 3-스트라이크",
                risk_config.min_rr_ratio, risk_config.max_daily_loss_pct)

    if args.autostart:
        logger.info("--autostart: 서버 준비 후 자동으로 로그인 및 매매를 시작합니다.")
        asyncio.create_task(scheduler.start())

    uv_config = uvicorn.Config(
        app,
        host=config.dashboard.host,
        port=config.dashboard.port,
        log_config=None,  # uvicorn이 로깅 설정을 덮어쓰지 않도록 (setup_logger 유지)
    )
    server = uvicorn.Server(uv_config)
    await server.serve()


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n서버를 종료합니다.")


if __name__ == "__main__":
    main()
