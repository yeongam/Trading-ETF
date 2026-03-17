"""다중 확인 매매 전략.

진입 조건: 아래 지표 중 3개 이상이 동시에 매수/매도 신호를 낼 때만 거래.
- RSI 과매도/과매수 반전
- MACD 골든크로스/데드크로스
- 볼린저 밴드 하단/상단 터치 후 반등/반락
- 이동평균선 정배열/역배열
- 스토캐스틱 골든크로스/데드크로스
- 거래량 급증 확인

리스크 관리:
- 리스크 매니저가 포지션 사이즈/손절/익절을 결정
- 이미 보유 중이면 청산 조건만 확인
"""

import logging
from dataclasses import dataclass, field

from ..broker.base import BaseBroker
from ..risk import RiskManager
from ..utils.indicators import (
    rsi, macd, bollinger_bands, sma, ema, stochastic, volume_ratio,
)
from .base import BaseStrategy, Signal, SignalType

logger = logging.getLogger(__name__)

# 매수/매도 확인에 필요한 최소 지표 동의 수
MIN_CONFIRMATIONS = 3


@dataclass
class PriceHistory:
    """종목별 가격 이력."""

    closes: list[float] = field(default_factory=list)
    highs: list[float] = field(default_factory=list)
    lows: list[float] = field(default_factory=list)
    volumes: list[int] = field(default_factory=list)
    max_size: int = 200

    def add(self, close: float, high: float, low: float, volume: int) -> None:
        self.closes.append(close)
        self.highs.append(high)
        self.lows.append(low)
        self.volumes.append(volume)
        if len(self.closes) > self.max_size:
            self.closes = self.closes[-self.max_size:]
            self.highs = self.highs[-self.max_size:]
            self.lows = self.lows[-self.max_size:]
            self.volumes = self.volumes[-self.max_size:]

    @property
    def ready(self) -> bool:
        return len(self.closes) >= 35  # MACD(26) + signal(9) 최소 필요


class MultiConfirmStrategy(BaseStrategy):
    """다중 지표 확인 전략.

    여러 기술적 지표가 동시에 같은 방향을 가리킬 때만 진입합니다.
    매매 빈도는 높지만, 확신 있는 시점에서만 거래합니다.
    """

    def __init__(self, risk_manager: RiskManager) -> None:
        self._risk = risk_manager
        self._histories: dict[str, PriceHistory] = {}

    def _get_history(self, code: str) -> PriceHistory:
        if code not in self._histories:
            self._histories[code] = PriceHistory()
        return self._histories[code]

    def _count_buy_signals(self, h: PriceHistory) -> tuple[int, list[str]]:
        """매수 신호 개수와 사유 목록 반환."""
        signals = []

        # 1. RSI 과매도 반전 (30 이하에서 올라올 때)
        rsi_val = rsi(h.closes, 14)
        if rsi_val is not None and 25 <= rsi_val <= 40:
            prev_rsi = rsi(h.closes[:-1], 14)
            if prev_rsi is not None and prev_rsi < rsi_val:
                signals.append(f"RSI 반등({rsi_val:.1f})")

        # 2. MACD 골든크로스 (히스토그램 양전환)
        macd_val = macd(h.closes)
        if macd_val is not None:
            _, _, hist = macd_val
            prev_macd = macd(h.closes[:-1])
            if prev_macd is not None:
                _, _, prev_hist = prev_macd
                if prev_hist < 0 and hist >= 0:
                    signals.append("MACD 골든크로스")

        # 3. 볼린저 밴드 하단 터치 후 반등
        bb = bollinger_bands(h.closes)
        if bb is not None:
            upper, middle, lower = bb
            price = h.closes[-1]
            prev_price = h.closes[-2] if len(h.closes) >= 2 else price
            if prev_price <= lower and price > lower:
                signals.append(f"BB 하단반등({lower:.0f})")
            elif price <= lower * 1.01:  # 하단 근접
                signals.append(f"BB 하단근접({lower:.0f})")

        # 4. 이동평균 정배열 전환 (5일선 > 20일선으로 돌파)
        sma5 = sma(h.closes, 5)
        sma20 = sma(h.closes, 20)
        if sma5 is not None and sma20 is not None:
            prev_sma5 = sma(h.closes[:-1], 5)
            prev_sma20 = sma(h.closes[:-1], 20)
            if prev_sma5 is not None and prev_sma20 is not None:
                if prev_sma5 <= prev_sma20 and sma5 > sma20:
                    signals.append("MA 정배열 전환")

        # 5. 스토캐스틱 골든크로스
        stoch = stochastic(h.highs, h.lows, h.closes)
        if stoch is not None:
            k, d = stoch
            prev_stoch = stochastic(h.highs[:-1], h.lows[:-1], h.closes[:-1])
            if prev_stoch is not None:
                prev_k, prev_d = prev_stoch
                if prev_k <= prev_d and k > d and k < 30:
                    signals.append(f"스토캐스틱 골든(%K={k:.1f})")

        # 6. 거래량 급증 (평균 대비 1.5배 이상)
        vol_r = volume_ratio(h.volumes)
        if vol_r is not None and vol_r >= 1.5:
            signals.append(f"거래량 급증({vol_r:.1f}x)")

        return len(signals), signals

    def _count_sell_signals(self, h: PriceHistory) -> tuple[int, list[str]]:
        """매도 신호 개수와 사유 목록 반환."""
        signals = []

        # 1. RSI 과매수 반전 (70 이상에서 내려올 때)
        rsi_val = rsi(h.closes, 14)
        if rsi_val is not None and 60 <= rsi_val <= 75:
            prev_rsi = rsi(h.closes[:-1], 14)
            if prev_rsi is not None and prev_rsi > rsi_val:
                signals.append(f"RSI 하락반전({rsi_val:.1f})")

        # 2. MACD 데드크로스
        macd_val = macd(h.closes)
        if macd_val is not None:
            _, _, hist = macd_val
            prev_macd = macd(h.closes[:-1])
            if prev_macd is not None:
                _, _, prev_hist = prev_macd
                if prev_hist > 0 and hist <= 0:
                    signals.append("MACD 데드크로스")

        # 3. 볼린저 밴드 상단 터치 후 반락
        bb = bollinger_bands(h.closes)
        if bb is not None:
            upper, middle, lower = bb
            price = h.closes[-1]
            prev_price = h.closes[-2] if len(h.closes) >= 2 else price
            if prev_price >= upper and price < upper:
                signals.append(f"BB 상단반락({upper:.0f})")

        # 4. 이동평균 역배열 전환
        sma5 = sma(h.closes, 5)
        sma20 = sma(h.closes, 20)
        if sma5 is not None and sma20 is not None:
            prev_sma5 = sma(h.closes[:-1], 5)
            prev_sma20 = sma(h.closes[:-1], 20)
            if prev_sma5 is not None and prev_sma20 is not None:
                if prev_sma5 >= prev_sma20 and sma5 < sma20:
                    signals.append("MA 역배열 전환")

        # 5. 스토캐스틱 데드크로스
        stoch = stochastic(h.highs, h.lows, h.closes)
        if stoch is not None:
            k, d = stoch
            prev_stoch = stochastic(h.highs[:-1], h.lows[:-1], h.closes[:-1])
            if prev_stoch is not None:
                prev_k, prev_d = prev_stoch
                if prev_k >= prev_d and k < d and k > 70:
                    signals.append(f"스토캐스틱 데드(%K={k:.1f})")

        return len(signals), signals

    async def analyze(self, code: str, broker: BaseBroker) -> Signal:
        """종목 분석 후 매매 신호 반환."""
        # 현재가 조회
        price_info = await broker.get_price(code)
        if not price_info:
            return Signal(type=SignalType.HOLD, code=code, reason="시세 조회 실패")

        # 가격 이력 갱신
        history = self._get_history(code)
        history.add(
            close=float(price_info.current_price),
            high=float(price_info.current_price),  # 토스증권에서 고/저가 제공 시 교체
            low=float(price_info.current_price),
            volume=price_info.volume,
        )

        current_price = price_info.current_price

        # 이미 보유 중이면 청산 조건만 확인
        if code in self._risk.positions:
            exit_reason = self._risk.check_exit(code, current_price)
            if exit_reason:
                position = self._risk.positions[code]
                return Signal(
                    type=SignalType.SELL,
                    code=code,
                    quantity=position.quantity,
                    price=0,
                    reason=exit_reason,
                )
            # 매도 신호 확인 (기술적 지표 기반)
            sell_count, sell_reasons = self._count_sell_signals(history)
            if sell_count >= MIN_CONFIRMATIONS:
                position = self._risk.positions[code]
                return Signal(
                    type=SignalType.SELL,
                    code=code,
                    quantity=position.quantity,
                    price=0,
                    reason=f"매도신호 {sell_count}개: {', '.join(sell_reasons)}",
                )
            return Signal(type=SignalType.HOLD, code=code, reason="보유 유지")

        # 데이터 충분한지 확인
        if not history.ready:
            return Signal(
                type=SignalType.HOLD, code=code,
                reason=f"데이터 수집 중 ({len(history.closes)}/35)",
            )

        # 매수 신호 확인
        can_open, deny_reason = self._risk.can_open_position(code)
        if not can_open:
            return Signal(type=SignalType.HOLD, code=code, reason=deny_reason)

        buy_count, buy_reasons = self._count_buy_signals(history)
        if buy_count >= MIN_CONFIRMATIONS:
            # 리스크 기반 수량 계산
            balance = await broker.get_balance()
            quantity = self._risk.calculate_position_size(balance, current_price)
            if quantity <= 0:
                return Signal(type=SignalType.HOLD, code=code, reason="잔고 부족")

            return Signal(
                type=SignalType.BUY,
                code=code,
                quantity=quantity,
                price=0,  # 시장가
                reason=f"매수신호 {buy_count}개: {', '.join(buy_reasons)}",
            )

        self._risk.consume_cooldown()
        return Signal(
            type=SignalType.HOLD, code=code,
            reason=f"매수신호 부족 ({buy_count}/{MIN_CONFIRMATIONS})",
        )
