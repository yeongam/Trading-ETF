"""리스크 관리 모듈.

핵심 원칙:
- 1회 매매 리스크: 총 자산의 최대 2%
- 손절가 도달 시 무조건 청산
- 트레일링 스탑으로 수익 보호
- 동시 보유 종목 수 제한
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class ActivePosition:
    """진행 중인 포지션 (손절/익절 관리용)."""

    code: str
    entry_price: int
    quantity: int
    stop_loss: float  # 손절가
    take_profit: float  # 익절가
    trailing_stop: float  # 트레일링 스탑 (최고가 대비 하락률)
    highest_price: float  # 진입 이후 최고가
    entry_time: str = field(default_factory=lambda: datetime.now().isoformat())

    def update_highest(self, current_price: float) -> None:
        """최고가 갱신."""
        if current_price > self.highest_price:
            self.highest_price = current_price

    def should_stop_loss(self, current_price: float) -> bool:
        """손절 조건 확인."""
        return current_price <= self.stop_loss

    def should_take_profit(self, current_price: float) -> bool:
        """익절 조건 확인."""
        return current_price >= self.take_profit

    def should_trailing_stop(self, current_price: float) -> bool:
        """트레일링 스탑 조건 확인.

        최고가 대비 trailing_stop% 이상 하락하면 청산.
        단, 진입가 이상일 때만 작동 (손실 방지).
        """
        if self.highest_price <= self.entry_price:
            return False
        drop_rate = (self.highest_price - current_price) / self.highest_price * 100
        return drop_rate >= self.trailing_stop

    def exit_reason(self, current_price: float) -> str | None:
        """청산 사유 반환. 청산 불필요 시 None."""
        self.update_highest(current_price)
        if self.should_stop_loss(current_price):
            return f"손절 (진입: {self.entry_price} → 현재: {current_price})"
        if self.should_take_profit(current_price):
            return f"익절 (진입: {self.entry_price} → 현재: {current_price})"
        if self.should_trailing_stop(current_price):
            return f"트레일링 스탑 (최고: {self.highest_price:.0f} → 현재: {current_price})"
        return None


@dataclass
class RiskConfig:
    """리스크 관리 설정."""

    max_risk_per_trade: float = 2.0  # 1회 매매 최대 리스크 (총자산 대비 %)
    stop_loss_pct: float = 1.5  # 손절 비율 (%)
    take_profit_pct: float = 3.0  # 익절 비율 (%)
    trailing_stop_pct: float = 1.0  # 트레일링 스탑 (최고가 대비 %)
    max_positions: int = 5  # 최대 동시 보유 종목 수
    max_daily_loss_pct: float = 5.0  # 일일 최대 손실률 (%)
    cooldown_after_loss: int = 3  # 연속 손실 후 쉬는 횟수


class RiskManager:
    """리스크 관리자."""

    def __init__(self, config: RiskConfig) -> None:
        self._config = config
        self._positions: dict[str, ActivePosition] = {}
        self._daily_pnl: float = 0.0
        self._consecutive_losses: int = 0
        self._cooldown_remaining: int = 0
        self._trade_count: int = 0
        self._win_count: int = 0

    @property
    def config(self) -> RiskConfig:
        return self._config

    @property
    def positions(self) -> dict[str, ActivePosition]:
        return dict(self._positions)

    @property
    def stats(self) -> dict:
        return {
            "active_positions": len(self._positions),
            "daily_pnl": self._daily_pnl,
            "consecutive_losses": self._consecutive_losses,
            "cooldown_remaining": self._cooldown_remaining,
            "total_trades": self._trade_count,
            "wins": self._win_count,
            "win_rate": (self._win_count / self._trade_count * 100) if self._trade_count > 0 else 0,
        }

    def can_open_position(self, code: str) -> tuple[bool, str]:
        """신규 포지션 진입 가능 여부 확인."""
        if code in self._positions:
            return False, f"이미 보유 중: {code}"
        if len(self._positions) >= self._config.max_positions:
            return False, f"최대 보유 종목 수 초과 ({self._config.max_positions})"
        if self._cooldown_remaining > 0:
            return False, f"쿨다운 중 (남은 횟수: {self._cooldown_remaining})"
        if self._daily_pnl <= -self._config.max_daily_loss_pct:
            return False, f"일일 최대 손실 도달 ({self._daily_pnl:.1f}%)"
        return True, "OK"

    def calculate_position_size(self, balance: int, entry_price: int) -> int:
        """리스크 기반 포지션 사이즈 계산.

        총 자산의 max_risk_per_trade%를 리스크로 잡고,
        손절 비율을 고려해 수량을 결정합니다.
        """
        if entry_price <= 0:
            return 0
        risk_amount = balance * (self._config.max_risk_per_trade / 100)
        loss_per_share = entry_price * (self._config.stop_loss_pct / 100)
        if loss_per_share <= 0:
            return 0
        quantity = int(risk_amount / loss_per_share)
        return max(1, quantity)

    def open_position(self, code: str, entry_price: int, quantity: int) -> ActivePosition:
        """새 포지션 등록."""
        stop_loss = entry_price * (1 - self._config.stop_loss_pct / 100)
        take_profit = entry_price * (1 + self._config.take_profit_pct / 100)

        position = ActivePosition(
            code=code,
            entry_price=entry_price,
            quantity=quantity,
            stop_loss=stop_loss,
            take_profit=take_profit,
            trailing_stop=self._config.trailing_stop_pct,
            highest_price=float(entry_price),
        )
        self._positions[code] = position
        logger.info(
            "포지션 진입: %s %d주 @ %d원 (손절: %.0f / 익절: %.0f)",
            code, quantity, entry_price, stop_loss, take_profit,
        )
        return position

    def check_exit(self, code: str, current_price: float) -> str | None:
        """포지션 청산 조건 확인. 청산 사유 반환, 불필요 시 None."""
        position = self._positions.get(code)
        if not position:
            return None
        return position.exit_reason(current_price)

    def close_position(self, code: str, exit_price: float) -> dict | None:
        """포지션 청산 처리."""
        position = self._positions.pop(code, None)
        if not position:
            return None

        pnl_pct = (exit_price - position.entry_price) / position.entry_price * 100
        pnl_amount = (exit_price - position.entry_price) * position.quantity

        self._trade_count += 1
        self._daily_pnl += pnl_pct

        if pnl_amount >= 0:
            self._win_count += 1
            self._consecutive_losses = 0
            self._cooldown_remaining = 0
        else:
            self._consecutive_losses += 1
            if self._consecutive_losses >= self._config.cooldown_after_loss:
                self._cooldown_remaining = self._config.cooldown_after_loss
                logger.warning(
                    "연속 %d회 손실. %d회 쿨다운 진입.",
                    self._consecutive_losses, self._cooldown_remaining,
                )

        result = {
            "code": code,
            "entry_price": position.entry_price,
            "exit_price": exit_price,
            "quantity": position.quantity,
            "pnl_pct": round(pnl_pct, 2),
            "pnl_amount": int(pnl_amount),
            "holding_time": position.entry_time,
        }
        logger.info(
            "포지션 청산: %s 수익률 %.2f%% (%+d원)",
            code, pnl_pct, int(pnl_amount),
        )
        return result

    def consume_cooldown(self) -> None:
        """쿨다운 1회 소모. 매 스케줄러 사이클마다 호출."""
        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1

    def reset_daily(self) -> None:
        """일일 통계 초기화 (매일 장 시작 시 호출)."""
        self._daily_pnl = 0.0
        self._consecutive_losses = 0
        self._cooldown_remaining = 0
