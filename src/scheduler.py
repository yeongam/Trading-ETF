"""매매 스케줄러 - 전략 실행 및 주문 처리를 관리합니다."""

import asyncio
import logging
from datetime import datetime

from .broker.base import BaseBroker, OrderResult
from .strategy.base import BaseStrategy, Signal, SignalType
from .risk import RiskManager
from .config import TradingConfig

logger = logging.getLogger(__name__)


class TradingScheduler:
    """주기적으로 전략을 실행하고 매매 신호에 따라 주문을 처리합니다."""

    def __init__(
        self,
        broker: BaseBroker,
        strategy: BaseStrategy,
        config: TradingConfig,
        risk_manager: RiskManager | None = None,
    ) -> None:
        self._broker = broker
        self._strategy = strategy
        self._config = config
        self._risk = risk_manager
        self._running = False
        self._trade_log: list[dict] = []

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def trade_log(self) -> list[dict]:
        return list(self._trade_log)

    @property
    def risk_stats(self) -> dict:
        if self._risk:
            return self._risk.stats
        return {}

    def _is_market_open(self) -> bool:
        """장 운영 시간 확인 (평일 09:00~15:30)."""
        now = datetime.now()
        if now.weekday() >= 5:  # 토, 일
            return False
        market_open = now.replace(hour=9, minute=0, second=0, microsecond=0)
        market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
        return market_open <= now <= market_close

    async def _execute_signal(self, signal: Signal) -> OrderResult | None:
        if signal.type == SignalType.HOLD:
            return None

        if self._config.dry_run:
            logger.info("[DRY RUN] %s %s %d주 @ %s - %s",
                        signal.type.value, signal.code, signal.quantity,
                        signal.price or "시장가", signal.reason)
            result = OrderResult(success=True, message=f"[DRY RUN] {signal.type.value}")
        elif signal.type == SignalType.BUY:
            result = await self._broker.buy(signal.code, signal.quantity, signal.price)
        else:
            result = await self._broker.sell(signal.code, signal.quantity, signal.price)

        # 리스크 매니저에 포지션 등록/청산
        if result.success and self._risk:
            price_info = await self._broker.get_price(signal.code)
            current_price = price_info.current_price if price_info else signal.price
            if signal.type == SignalType.BUY:
                self._risk.open_position(signal.code, current_price, signal.quantity)
            elif signal.type == SignalType.SELL:
                self._risk.close_position(signal.code, current_price)

        self._trade_log.append({
            "timestamp": datetime.now().isoformat(),
            "type": signal.type.value,
            "code": signal.code,
            "quantity": signal.quantity,
            "price": signal.price,
            "reason": signal.reason,
            "dry_run": self._config.dry_run,
            "success": result.success,
            "message": result.message,
        })
        return result

    async def run_once(self) -> list[dict]:
        """감시 종목에 대해 전략을 1회 실행합니다."""
        results = []
        for code in self._config.watchlist:
            try:
                signal = await self._strategy.analyze(code, self._broker)
                logger.info("[%s] %s -> %s (%s)", self._strategy.name, code,
                            signal.type.value, signal.reason)
                result = await self._execute_signal(signal)
                results.append({
                    "code": code,
                    "signal": signal.type.value,
                    "reason": signal.reason,
                    "executed": result is not None,
                })
            except Exception as e:
                logger.error("전략 실행 오류 [%s]: %s", code, e)
                results.append({"code": code, "signal": "error", "reason": str(e)})
        return results

    async def start(self) -> None:
        """스케줄러를 시작합니다. check_interval 간격으로 전략을 반복 실행합니다."""
        self._running = True
        logger.info("스케줄러 시작 (전략: %s, 간격: %d초, 종목: %s)",
                     self._strategy.name, self._config.check_interval,
                     self._config.watchlist)

        while self._running:
            if self._is_market_open():
                await self.run_once()
            else:
                logger.debug("장 운영 시간이 아닙니다.")
            await asyncio.sleep(self._config.check_interval)

    def stop(self) -> None:
        """스케줄러를 중지합니다."""
        self._running = False
        logger.info("스케줄러 중지")
