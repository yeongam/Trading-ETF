"""리스크 관리 모듈 — 상위 1% 미주 단타 SOP 기준.

핵심 원칙:
- 1회 매매 리스크: 총 자산의 최대 1% 손실 제한
- 진입 수량: (총자산 × 0.01) / (진입가 − 손절가)
- 손익비(R:R): 기대 수익 ≥ 리스크 × 2 (1:2+)
- 데일리 컷: 당일 총자산 대비 -2% 손실 시 즉각 종료
- 3-스트라이크: 연속 3회 손절 시 강제 휴식
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class ActivePosition:
    """진행 중인 포지션."""

    code: str
    entry_price: int
    quantity: int
    stop_loss: float  # 동적 손절가 (전략이 계산)
    take_profit: float  # 동적 익절가 (전략이 계산)
    highest_price: float  # 진입 이후 최고가
    entry_strategy: str = ""  # "A" (PMH 돌파) / "B" (EMA 추세)
    entry_time: str = field(default_factory=lambda: datetime.now().isoformat())

    def update_highest(self, current_price: float) -> None:
        """최고가 갱신."""
        if current_price > self.highest_price:
            self.highest_price = current_price

    def should_stop_loss(self, current_price: float) -> bool:
        """손절 조건: 손절가 이하."""
        return current_price <= self.stop_loss

    def should_take_profit(self, current_price: float) -> bool:
        """익절 조건: 익절가 이상."""
        return current_price >= self.take_profit

    def hard_exit_reason(self, current_price: float) -> str | None:
        """하드 청산 사유 (손절/익절). 전략 기반 청산은 strategy에서 처리."""
        self.update_highest(current_price)
        if self.should_stop_loss(current_price):
            loss_pct = (self.entry_price - current_price) / self.entry_price * 100
            return f"손절 (진입: {self.entry_price:,} → 현재: {current_price:,}, -{loss_pct:.1f}%)"
        if self.should_take_profit(current_price):
            gain_pct = (current_price - self.entry_price) / self.entry_price * 100
            return f"익절 (진입: {self.entry_price:,} → 현재: {current_price:,}, +{gain_pct:.1f}%)"
        return None


@dataclass
class RiskConfig:
    """리스크 관리 설정 — SOP 기반."""

    max_risk_per_trade: float = 1.0  # 1회 매매 최대 리스크 (총자산 대비 %)
    min_rr_ratio: float = 2.0  # 최소 손익비 (R:R) — 2 이상만 진입
    max_positions: int = 5  # 최대 동시 보유 종목 수
    max_daily_loss_pct: float = 2.0  # 데일리 컷 (총자산 대비 %)
    cooldown_after_loss: int = 3  # 3-스트라이크 후 강제 휴식 횟수
    max_position_pct: float = 25.0  # 종목당 최대 투자 비중 (총자산 대비 %)
    min_stop_distance_pct: float = 1.5  # 최소 손절 거리 (진입가 대비 %)
    stop_confirm_ticks: int = 2  # 손절 연속 확인 횟수 (노이즈 방지)


class RiskManager:
    """리스크 관리자 — SOP 기반."""

    def __init__(self, config: RiskConfig, total_budget: int = 100_000) -> None:
        self._config = config
        self._total_budget = total_budget
        self._initial_budget = total_budget
        self._total_invested: int = 0
        self._positions: dict[str, ActivePosition] = {}
        self._daily_pnl: float = 0.0
        self._consecutive_losses: int = 0
        self._cooldown_remaining: int = 0
        self._trade_count: int = 0
        self._win_count: int = 0
        self._loss_count: int = 0
        self._stop_confirm: dict[str, int] = {}  # 손절 연속 확인 카운터

    @property
    def config(self) -> RiskConfig:
        return self._config

    @property
    def positions(self) -> dict[str, ActivePosition]:
        """현재 보유 포지션 (읽기 전용 참조 — 직접 수정 금지)."""
        return self._positions

    @property
    def stats(self) -> dict:
        return {
            "active_positions": len(self._positions),
            "position_codes": list(self._positions.keys()),
            "daily_pnl": self._daily_pnl,
            "consecutive_losses": self._consecutive_losses,
            "cooldown_remaining": self._cooldown_remaining,
            "total_trades": self._trade_count,
            "wins": self._win_count,
            "losses": self._loss_count,
            "win_rate": (self._win_count / (self._win_count + self._loss_count) * 100) if (self._win_count + self._loss_count) > 0 else 0,
            "total_budget": self._total_budget,
            "total_invested": self._total_invested,
            "remaining_budget": self._total_budget - self._total_invested,
        }

    def can_open_position(self, code: str) -> tuple[bool, str]:
        """신규 포지션 진입 가능 여부 확인."""
        if code in self._positions:
            return False, f"이미 보유 중: {code}"
        if len(self._positions) >= self._config.max_positions:
            return False, f"최대 보유 종목 수 초과 ({self._config.max_positions})"
        if self._cooldown_remaining > 0:
            return False, f"3-스트라이크 쿨다운 중 (남은: {self._cooldown_remaining})"
        if self._daily_pnl <= -self._config.max_daily_loss_pct:
            return False, f"데일리 컷 발동 ({self._daily_pnl:.1f}% ≤ -{self._config.max_daily_loss_pct}%)"
        if self._total_invested >= self._total_budget:
            return False, f"예산 소진 (투자: {self._total_invested:,}원 / 예산: {self._total_budget:,}원)"
        return True, "OK"

    def check_rr_ratio(self, entry_price: float, stop_loss: float, take_profit: float) -> tuple[bool, float]:
        """손익비(R:R) 검증.

        Returns:
            (통과 여부, 실제 R:R 비율)
        """
        risk = entry_price - stop_loss
        if risk <= 0:
            return False, 0.0
        reward = take_profit - entry_price
        if reward <= 0:
            return False, 0.0
        rr = reward / risk
        return rr >= self._config.min_rr_ratio, round(rr, 2)

    def calculate_position_size(self, entry_price: int, stop_loss: float) -> int:
        """SOP 수량 공식: (총자산 × 0.01) / (진입가 − 손절가).

        3단계 제한:
        1. 리스크 기반: 1회 최대 손실 1% 이내
        2. 종목 비중: 한 종목에 총자산의 max_position_pct% 이상 투입 금지
        3. 잔여 예산: 남은 예산 초과 금지
        """
        if entry_price <= 0:
            return 0
        risk_per_share = entry_price - stop_loss
        if risk_per_share <= 0:
            return 0

        # 1. 리스크 기반 수량 (SOP 공식)
        risk_amount = self._total_budget * (self._config.max_risk_per_trade / 100)
        risk_qty = risk_amount / risk_per_share

        # 2. 종목당 최대 투자 비중 제한
        max_invest = self._total_budget * (self._config.max_position_pct / 100)
        position_qty = max_invest / entry_price

        # 3. 잔여 예산 제한
        remaining = self._total_budget - self._total_invested
        if remaining <= 0:
            return 0
        budget_qty = remaining / entry_price

        qty = int(min(risk_qty, position_qty, budget_qty))
        # 비중 초과 시에도 최소 1주는 매수
        if qty <= 0 and remaining >= entry_price:
            qty = 1
        return max(0, qty)

    def open_position(
        self,
        code: str,
        entry_price: int,
        quantity: int,
        stop_loss: float = 0,
        take_profit: float = 0,
        entry_strategy: str = "",
    ) -> ActivePosition:
        """새 포지션 등록.

        stop_loss가 진입가 대비 min_stop_distance_pct 미만이면 강제로 넓힙니다.
        """
        # 최소 손절 거리 강제 (너무 타이트한 손절 → 노이즈에 발동 방지)
        min_stop = entry_price * (1 - self._config.min_stop_distance_pct / 100)
        if stop_loss > min_stop:
            logger.info(
                "손절 거리 조정 [%s]: %s → %s (최소 %.1f%%)",
                code, f"{stop_loss:,.0f}", f"{min_stop:,.0f}",
                self._config.min_stop_distance_pct,
            )
            stop_loss = min_stop
            # 익절가도 R:R 비율에 맞게 재조정
            risk = entry_price - stop_loss
            take_profit = max(take_profit, entry_price + risk * self._config.min_rr_ratio)

        position = ActivePosition(
            code=code,
            entry_price=entry_price,
            quantity=quantity,
            stop_loss=stop_loss,
            take_profit=take_profit,
            highest_price=float(entry_price),
            entry_strategy=entry_strategy,
        )
        self._positions[code] = position
        self._total_invested += int(entry_price * quantity)
        risk = entry_price - stop_loss if stop_loss > 0 else 0
        reward = take_profit - entry_price if take_profit > 0 else 0
        rr = reward / risk if risk > 0 else 0
        logger.info(
            "포지션 진입[%s]: %s %d주 @ %s원 (손절: %s / 익절: %s / R:R %.1f) [투자: %s원 / 예산: %s원]",
            entry_strategy or "?", code, quantity, f"{entry_price:,}",
            f"{stop_loss:,.0f}", f"{take_profit:,.0f}", rr,
            f"{self._total_invested:,}", f"{self._total_budget:,}",
        )
        return position

    def check_exit(self, code: str, current_price: float) -> str | None:
        """하드 청산 조건 확인 (손절/익절).

        손절은 연속 N틱 확인 후 발동 (노이즈 방지).
        익절은 즉시 발동.
        """
        position = self._positions.get(code)
        if not position:
            return None

        # 최고가 갱신
        position.update_highest(current_price)

        # 익절: 즉시 발동 (수익은 빨리 확보)
        if position.should_take_profit(current_price):
            self._stop_confirm.pop(code, None)
            gain_pct = (current_price - position.entry_price) / position.entry_price * 100
            return f"익절 (진입: {position.entry_price:,} → 현재: {current_price:,}, +{gain_pct:.1f}%)"

        # 손절: 연속 확인 (노이즈에 의한 0.x% 손절 방지)
        if position.should_stop_loss(current_price):
            self._stop_confirm[code] = self._stop_confirm.get(code, 0) + 1
            if self._stop_confirm[code] >= self._config.stop_confirm_ticks:
                self._stop_confirm.pop(code, None)
                loss_pct = (position.entry_price - current_price) / position.entry_price * 100
                return f"손절 (진입: {position.entry_price:,} → 현재: {current_price:,}, -{loss_pct:.1f}%)"
            logger.debug(
                "손절 확인 중 [%s]: %d/%d (현재 %s ≤ 손절 %s)",
                code, self._stop_confirm[code], self._config.stop_confirm_ticks,
                f"{current_price:,}", f"{position.stop_loss:,.0f}",
            )
            return None

        # 손절가 위에 있으면 카운터 리셋
        self._stop_confirm.pop(code, None)
        return None

    def close_position(self, code: str, exit_price: float) -> dict | None:
        """포지션 청산 처리."""
        position = self._positions.pop(code, None)
        if not position:
            return None

        self._total_invested = max(0, self._total_invested - int(position.entry_price * position.quantity))

        pnl_pct = (exit_price - position.entry_price) / position.entry_price * 100
        pnl_amount = (exit_price - position.entry_price) * position.quantity

        # 포트폴리오 대비 손익 비중 (총자산 기준)
        portfolio_impact = (pnl_amount / self._total_budget * 100) if self._total_budget > 0 else 0

        self._trade_count += 1
        self._daily_pnl += portfolio_impact

        # 손절 확인 카운터 정리
        self._stop_confirm.pop(code, None)

        if pnl_amount > 0:
            self._win_count += 1
            self._consecutive_losses = 0
            self._cooldown_remaining = 0
        elif pnl_amount < 0:
            self._loss_count += 1
            self._consecutive_losses += 1
            if self._consecutive_losses >= self._config.cooldown_after_loss:
                self._cooldown_remaining = self._config.cooldown_after_loss
                logger.warning(
                    "3-스트라이크! 연속 %d회 손절 → %d사이클 강제 휴식.",
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
        """쿨다운 1회 소모. 쿨다운 완료 시 연속 손실 카운터도 리셋."""
        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            if self._cooldown_remaining == 0:
                self._consecutive_losses = 0

    def update_budget(self, new_budget: int) -> None:
        """운용 금액 변경."""
        old = self._total_budget
        self._total_budget = new_budget
        logger.info("운용 금액 변경: %s원 → %s원", f"{old:,}", f"{new_budget:,}")

    def reset_daily(self) -> None:
        """일일 통계 초기화."""
        self._daily_pnl = 0.0
        self._consecutive_losses = 0
        self._cooldown_remaining = 0
