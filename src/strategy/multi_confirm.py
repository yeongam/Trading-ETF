"""상위 1% 미주 단타 전략 — SOP 기반.

전략 A: 세션 고점(PMH) 돌파 후 눌림 매매
    - 세션 중 형성된 고점을 돌파한 뒤, 다시 내려와 PMH/VWAP 지지 확인 시 진입.
    - 익절: 1차 목표(R:R 2:1+), 잔여 물량 9 EMA 이탈 시 청산.
    - 손절: PMH/VWAP 하향 이탈 시 즉시 청산.

전략 B: 9 EMA / 20 EMA 추세 추종
    - 상승 추세에서 거래량 감소하며 눌림 발생 → EMA 터치 후 반등 시 진입.
    - 익절: 전고점 돌파 시 일부, 잔여 물량 20 EMA 이탈 시 청산.
    - 손절: 전저점 또는 20 EMA 이탈 시 즉시 청산.

공통 필터:
    1. 현재가 > VWAP (매수 관점에서만 진입)
    2. RVOL ≥ 2.0 (거래량 200% 이상 폭발)
    3. R:R ≥ 2:1 (리스크 매니저 검증)

시간대별 시장 대응:
    - Pre-Market (04:00~09:30 ET): 전략 A 위주 (뉴스 재료 돌파)
    - Regular Open (09:30~10:30 ET): A+B 모두 (최대 유동성)
    - Mid-Day (10:30~16:00 ET): 전략 B 위주 (돌파 지양, 저점 지지 확인)
    - After-Hours (16:00~20:00 ET): 홀드 (실적 발표 변동성)
    - Closed: 매매 안함

뇌동매매 방지:
    - 진입/청산 시그널은 연속 N틱 확인 후 실행 (1틱 노이즈 무시)
    - 최소 보유 시간: 매수 후 일정 사이클 동안 청산 금지
    - 의미 있는 가격 변동만 반등/하락으로 인식 (노이즈 필터)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from ..broker.base import BaseBroker
from ..risk import RiskManager
from ..utils.indicators import ema, sma, vwap, rvol
from .base import BaseStrategy, Signal, SignalType

logger = logging.getLogger(__name__)

# ── 상수 ──────────────────────────────────────────────────────────────────────

MIN_RVOL = 2.0           # RVOL 최소 기준 (200%)
TAKE_PROFIT_PCT = 3.0    # 1차 익절 목표 (%)
PMH_BUFFER_PCT = 0.3     # PMH 지지 확인 버퍼 (%)
ET = ZoneInfo("America/New_York")

# ── 뇌동매매 방지 상수 ────────────────────────────────────────────────────────

MIN_HOLD_CYCLES = 6           # 매수 후 최소 보유 사이클 (청산 금지 기간)
ENTRY_CONFIRM_TICKS = 3       # 진입 조건 연속 확인 횟수
EXIT_CONFIRM_TICKS = 3        # 청산 조건 연속 확인 횟수
NOISE_THRESHOLD_PCT = 0.15    # 이 비율 이하의 가격 변동은 노이즈로 무시 (%)
MIN_BOUNCE_PCT = 0.3          # 반등으로 인정하는 최소 변동폭 (%)
EMA_EXIT_BUFFER_PCT = 0.5    # EMA 이탈로 인정하는 최소 버퍼 (%)


# ── 시장 국면 ─────────────────────────────────────────────────────────────────

def get_market_phase() -> str:
    """현재 미국 동부시간 기준 시장 국면 반환."""
    now = datetime.now(ET)
    t = now.hour * 60 + now.minute  # 자정 기준 분

    if 240 <= t < 570:    # 04:00~09:30
        return "premarket"
    elif 570 <= t < 630:  # 09:30~10:30
        return "open"
    elif 630 <= t < 960:  # 10:30~16:00
        return "midday"
    elif 960 <= t < 1200: # 16:00~20:00
        return "afterhours"
    return "closed"


# ── 가격 이력 ─────────────────────────────────────────────────────────────────

@dataclass
class PriceHistory:
    """종목별 가격 이력."""

    closes: list[float] = field(default_factory=list)
    highs: list[float] = field(default_factory=list)
    lows: list[float] = field(default_factory=list)
    volumes: list[int] = field(default_factory=list)
    session_high: float = 0  # 세션 중 최고가 (PMH 대용)
    session_date: str = ""  # 세션 날짜 (날짜 변경 시 리셋)
    session_start_idx: int = 0  # 세션 시작 인덱스 (VWAP 세션 기반 계산용)
    max_size: int = 200

    def add(self, close: float, high: float, low: float, volume: int) -> None:
        # 세션 날짜 변경 시 session_high 리셋 + VWAP 시작점 기록
        today = datetime.now(ET).strftime("%Y-%m-%d")
        if self.session_date != today:
            self.session_date = today
            self.session_high = 0
            self.session_start_idx = len(self.closes)

        self.closes.append(close)
        self.highs.append(high)
        self.lows.append(low)
        self.volumes.append(volume)
        if close > self.session_high:
            self.session_high = close
        if len(self.closes) > self.max_size:
            trim = len(self.closes) - self.max_size
            self.closes = self.closes[-self.max_size:]
            self.highs = self.highs[-self.max_size:]
            self.lows = self.lows[-self.max_size:]
            self.volumes = self.volumes[-self.max_size:]
            self.session_start_idx = max(0, self.session_start_idx - trim)

    @property
    def session_closes(self) -> list[float]:
        """현재 세션의 종가 리스트."""
        return self.closes[self.session_start_idx:]

    @property
    def session_volumes(self) -> list[int]:
        """현재 세션의 거래량 리스트."""
        return self.volumes[self.session_start_idx:]

    @property
    def recent_low(self) -> float:
        """최근 5봉 중 최저가."""
        if len(self.closes) < 5:
            return min(self.closes) if self.closes else 0
        return min(self.closes[-5:])

    def pct_change(self, lookback: int = 1) -> float:
        """최근 가격 변동률 (%)."""
        if len(self.closes) < lookback + 1:
            return 0.0
        old = self.closes[-(lookback + 1)]
        if old == 0:
            return 0.0
        return (self.closes[-1] - old) / old * 100

    def trend_direction(self, lookback: int = 5) -> str:
        """최근 N틱 추세 방향.

        Returns:
            'up' / 'down' / 'sideways'
        """
        if len(self.closes) < lookback + 1:
            return "sideways"
        start = self.closes[-(lookback + 1)]
        end = self.closes[-1]
        if start == 0:
            return "sideways"
        pct = (end - start) / start * 100
        if pct > NOISE_THRESHOLD_PCT:
            return "up"
        elif pct < -NOISE_THRESHOLD_PCT:
            return "down"
        return "sideways"


# ── 전략 클래스 ───────────────────────────────────────────────────────────────

class MultiConfirmStrategy(BaseStrategy):
    """상위 1% 미주 단타 전략 (SOP).

    VWAP + EMA(9/20) + RVOL 필터 → 전략 A(PMH 돌파) / 전략 B(EMA 추세) 진입.
    21사이클 데이터로 동작합니다 (EMA 20 최소 요구).

    뇌동매매 방지:
    - 연속 확인: 진입/청산 시그널이 N틱 연속 유지되어야 실행
    - 최소 보유: 매수 후 MIN_HOLD_CYCLES 동안 동적 청산 금지
    - 노이즈 필터: 의미 없는 가격 변동은 무시
    """

    def __init__(self, risk_manager: RiskManager) -> None:
        self._risk = risk_manager
        self._histories: dict[str, PriceHistory] = {}
        # 연속 확인 카운터: 시그널이 N틱 연속 유지되어야 실행
        self._entry_confirm: dict[str, int] = {}   # code → 연속 진입 시그널 횟수
        self._exit_confirm: dict[str, int] = {}    # code → 연속 청산 시그널 횟수
        # 보유 시작 사이클 (최소 보유 기간 추적)
        self._hold_start: dict[str, int] = {}      # code → 매수 시점 사이클 번호
        self._cycle_count: int = 0

    def get_data_count(self, code: str) -> int:
        """종목의 현재 데이터 수집 횟수 반환."""
        h = self._histories.get(code)
        return len(h.closes) if h else 0

    def _get_history(self, code: str) -> PriceHistory:
        if code not in self._histories:
            self._histories[code] = PriceHistory()
        return self._histories[code]

    # ── 3초 체크리스트 (공통 필터) ────────────────────────────────────────────

    def _pass_checklist(self, h: PriceHistory, current_price: float) -> tuple[bool, str]:
        """매수 전 3초 체크리스트.

        1. 현재가 > VWAP?
        2. RVOL ≥ 2.0?
        """
        # 1. VWAP 위에 있는가? (세션 기반)
        vwap_val = vwap(h.session_closes, h.session_volumes)
        if vwap_val is None:
            # VWAP 계산 불가 (거래량 0 또는 데이터 부족) → SMA 대체
            sma_val = sma(h.closes, min(10, len(h.closes)))
            if sma_val is None:
                return False, "VWAP/SMA 계산 불가"
            if current_price <= sma_val:
                return False, f"SMA 하회 (현재 {current_price:,} ≤ SMA {sma_val:,.0f})"
            vwap_val = sma_val  # 이후 로그용
        elif current_price <= vwap_val:
            return False, f"VWAP 하회 (현재 {current_price:,} ≤ VWAP {vwap_val:,.0f})"

        # 2. RVOL ≥ 2.0? (거래량 데이터 없으면 필터 통과)
        rvol_val = rvol(h.volumes, period=10)
        has_volume = any(v > 0 for v in h.volumes)
        if not has_volume:
            # 차트에서 거래량을 못 읽는 경우 — RVOL 필터 스킵
            return True, f"VWAP+{current_price - vwap_val:,.0f} (거래량 데이터 없음)"
        if rvol_val is None:
            return False, "RVOL 계산 불가"
        if rvol_val < MIN_RVOL:
            return False, f"RVOL 부족 ({rvol_val:.1f}x < {MIN_RVOL}x)"

        return True, f"VWAP+{current_price - vwap_val:,.0f} RVOL {rvol_val:.1f}x"

    # ── 전략 A: PMH 돌파 후 눌림 ─────────────────────────────────────────────

    def _check_strategy_a(self, h: PriceHistory, current_price: float) -> tuple[bool, float, str]:
        """전략 A: PMH 돌파 후 VWAP/PMH 지지 확인.

        Returns:
            (진입 여부, 손절가, 사유)
        """
        if len(h.closes) < 15:
            return False, 0, ""

        pmh = h.session_high
        if pmh <= 0:
            return False, 0, ""

        vwap_val = vwap(h.session_closes, h.session_volumes)
        if vwap_val is None:
            return False, 0, ""

        # 조건 1: 과거에 PMH를 실제로 돌파한 이력 (근사치가 아닌 실제 도달)
        had_breakout = any(p >= pmh for p in h.closes[-15:-3])
        if not had_breakout:
            return False, 0, ""

        # 조건 2: 돌파 후 눌림이 발생 (현재가 < PMH)
        if current_price >= pmh:
            return False, 0, ""  # 아직 눌림 미발생

        # 조건 3: 지지 레벨 근방에서 반등 — 의미 있는 반등만
        support = max(pmh * (1 - PMH_BUFFER_PCT / 100), vwap_val)

        # 최근 5틱 중 지지선 터치 이력 + 현재 반등 중
        touched_support = any(
            p <= support * 1.002 for p in h.closes[-5:-1]
        )
        if not touched_support:
            return False, 0, ""

        # 반등 확인: 최근 3틱 추세가 상승이어야 함 (1틱 노이즈 제거)
        if h.trend_direction(3) != "up":
            return False, 0, ""

        # 반등 강도: 최저점에서 최소 MIN_BOUNCE_PCT 이상 올라와야 함
        recent_bottom = min(h.closes[-5:])
        bounce_pct = (current_price - recent_bottom) / recent_bottom * 100 if recent_bottom > 0 else 0
        if bounce_pct < MIN_BOUNCE_PCT:
            return False, 0, ""

        # 손절가: 지지선(VWAP/PMH) 아래
        stop_loss = min(vwap_val, support) * 0.997

        return True, stop_loss, f"PMH돌파눌림 (PMH {pmh:,.0f} / 지지 {support:,.0f} / 반등 {bounce_pct:.1f}%)"

    # ── 전략 B: EMA 추세 추종 ────────────────────────────────────────────────

    def _check_strategy_b(self, h: PriceHistory, current_price: float) -> tuple[bool, float, str]:
        """전략 B: 9/20 EMA 추세에서 눌림 후 반등.

        Returns:
            (진입 여부, 손절가, 사유)
        """
        if len(h.closes) < 21:
            return False, 0, ""

        ema9 = ema(h.closes, 9)
        ema20 = ema(h.closes, 20)
        if ema9 is None or ema20 is None:
            return False, 0, ""

        # 조건 1: 상승 추세 (9 EMA > 20 EMA) + 의미 있는 격차
        ema_gap_pct = (ema9 - ema20) / ema20 * 100 if ema20 > 0 else 0
        if ema_gap_pct < 0.1:  # EMA 격차가 0.1% 미만이면 추세 불분명
            return False, 0, ""

        # 조건 2: 최근 눌림 이력 — 최근 5틱 중 EMA9 또는 EMA20까지 내려온 적 있어야
        touched_ema9 = any(p <= ema9 * 1.001 for p in h.closes[-5:-1])
        touched_ema20 = any(p <= ema20 * 1.001 for p in h.closes[-5:-1])

        if touched_ema20:
            ema_support = ema20
            support_name = "20EMA"
        elif touched_ema9:
            ema_support = ema9
            support_name = "9EMA"
        else:
            return False, 0, ""  # 눌림 없이 고점만 유지 중 → 추격매수 방지

        # 조건 3: 반등 확인 — 최근 3틱 추세가 상승
        if h.trend_direction(3) != "up":
            return False, 0, ""

        # 조건 4: 반등 강도 확인
        recent_bottom = min(h.closes[-5:])
        bounce_pct = (current_price - recent_bottom) / recent_bottom * 100 if recent_bottom > 0 else 0
        if bounce_pct < MIN_BOUNCE_PCT:
            return False, 0, ""

        # 조건 5: 현재가가 EMA 지지선 위에 있어야 (아래에 있으면 반등 실패)
        if current_price < ema_support:
            return False, 0, ""

        # 손절가: 20 EMA 아래 또는 최근 저점
        stop_loss = min(ema20, h.recent_low) * 0.997

        return True, stop_loss, f"EMA추세반등 ({support_name} {ema_support:,.0f} 터치→반등 {bounce_pct:.1f}%)"

    # ── 보유 종목 청산 조건 ───────────────────────────────────────────────────

    def _check_dynamic_exit(self, code: str, h: PriceHistory, current_price: float) -> str | None:
        """전략 기반 동적 청산 (EMA 이탈).

        전략 A: 9 EMA 이탈 시 청산.
        전략 B: 20 EMA 이탈 시 청산.

        뇌동매매 방지:
        - 최소 보유 기간 미충족 시 하드 손절/익절만 적용 (동적 청산 보류)
        - EMA 이탈은 버퍼 + 연속 확인으로 판단
        """
        position = self._risk.positions.get(code)
        if not position:
            return None

        # 최소 보유 기간 체크 — 미충족 시 동적 청산 보류
        hold_start = self._hold_start.get(code, 0)
        held_cycles = self._cycle_count - hold_start
        if held_cycles < MIN_HOLD_CYCLES:
            return None  # 하드 손절/익절은 RiskManager에서 별도 처리

        # EMA 이탈 판단 (버퍼 적용 — 노이즈 제거)
        exit_triggered = False
        exit_msg = ""

        if position.entry_strategy == "A":
            ema9 = ema(h.closes, 9)
            if ema9:
                threshold = ema9 * (1 - EMA_EXIT_BUFFER_PCT / 100)
                if current_price < threshold:
                    exit_triggered = True
                    exit_msg = f"9EMA 이탈 (현재 {current_price:,} < 9EMA-{EMA_EXIT_BUFFER_PCT}% {threshold:,.0f})"
        elif position.entry_strategy == "B":
            ema20 = ema(h.closes, 20)
            if ema20:
                threshold = ema20 * (1 - EMA_EXIT_BUFFER_PCT / 100)
                if current_price < threshold:
                    exit_triggered = True
                    exit_msg = f"20EMA 이탈 (현재 {current_price:,} < 20EMA-{EMA_EXIT_BUFFER_PCT}% {threshold:,.0f})"

        if not exit_triggered:
            # 청산 조건 미충족 → 카운터 리셋
            self._exit_confirm[code] = 0
            return None

        # 연속 확인: N틱 연속 이탈 시에만 실제 청산
        self._exit_confirm[code] = self._exit_confirm.get(code, 0) + 1
        if self._exit_confirm[code] < EXIT_CONFIRM_TICKS:
            logger.debug(
                "청산 신호 확인 중 [%s]: %d/%d — %s",
                code, self._exit_confirm[code], EXIT_CONFIRM_TICKS, exit_msg,
            )
            return None

        # N틱 연속 확인 완료 → 청산 실행
        self._exit_confirm[code] = 0
        return exit_msg

    # ── 메인 분석 ────────────────────────────────────────────────────────────

    def advance_cycle(self) -> None:
        """스케줄러 사이클 1회 진행. 매 사이클 시작 시 한 번만 호출해야 합니다."""
        self._cycle_count += 1

    async def analyze(self, code: str, broker: BaseBroker, min_data: int = 21) -> Signal:
        """종목 분석 후 매매 신호 반환."""

        # 현재가 조회
        price_info = await broker.get_price(code)
        if not price_info:
            return Signal(type=SignalType.HOLD, code=code, reason="시세 조회 실패")

        # 가격 이력 갱신
        history = self._get_history(code)
        history.add(
            close=float(price_info.current_price),
            high=float(price_info.current_price),
            low=float(price_info.current_price),
            volume=price_info.volume,
        )

        current_price = price_info.current_price

        # ── 이미 보유 중: 청산 조건 확인 ──
        if code in self._risk.positions:
            # 1. 하드 청산 (손절/익절) — 리스크 매니저 (최소 보유 기간 무시, 항상 적용)
            exit_reason = self._risk.check_exit(code, current_price)
            if exit_reason:
                position = self._risk.positions[code]
                self._exit_confirm.pop(code, None)
                self._hold_start.pop(code, None)
                return Signal(
                    type=SignalType.SELL,
                    code=code,
                    quantity=position.quantity,
                    price=0,
                    reason=exit_reason,
                )

            # 2. 동적 청산 (EMA 이탈) — 전략 기반 (최소 보유 + 연속 확인)
            dynamic_reason = self._check_dynamic_exit(code, history, current_price)
            if dynamic_reason:
                position = self._risk.positions[code]
                self._exit_confirm.pop(code, None)
                self._hold_start.pop(code, None)
                return Signal(
                    type=SignalType.SELL,
                    code=code,
                    quantity=position.quantity,
                    price=0,
                    reason=dynamic_reason,
                )

            # 보유 현황 로그
            hold_start = self._hold_start.get(code, self._cycle_count)
            held = self._cycle_count - hold_start
            return Signal(
                type=SignalType.HOLD, code=code,
                reason=f"보유 유지 (보유 {held}사이클)",
            )

        # ── 데이터 충분한지 확인 ──
        if len(history.closes) < min_data:
            return Signal(
                type=SignalType.HOLD, code=code,
                reason=f"데이터 수집 중 ({len(history.closes)}/{min_data})",
            )

        # ── 시장 국면 확인 ──
        phase = get_market_phase()
        if phase == "closed":
            return Signal(type=SignalType.HOLD, code=code, reason="장외 시간")
        if phase == "afterhours":
            return Signal(type=SignalType.HOLD, code=code, reason="애프터마켓 — 관망")

        # ── 포지션 진입 가능 여부 ──
        can_open, deny_reason = self._risk.can_open_position(code)
        if not can_open:
            return Signal(type=SignalType.HOLD, code=code, reason=deny_reason)

        # ── 추세 방향 확인 — 하락 추세에서는 진입 금지 ──
        trend = history.trend_direction(10)
        if trend == "down":
            self._entry_confirm[code] = 0  # 하락 추세 → 진입 카운터 리셋
            return Signal(type=SignalType.HOLD, code=code, reason=f"하락 추세 — 진입 보류")

        # ── 3초 체크리스트 (VWAP + RVOL) ──
        passed, filter_msg = self._pass_checklist(history, current_price)
        if not passed:
            self._entry_confirm[code] = 0  # 체크리스트 실패 → 카운터 리셋
            return Signal(type=SignalType.HOLD, code=code, reason=filter_msg)

        # ── 전략 A 또는 B 진입 시도 ──
        # 시간대별 전략 우선순위
        strategies: list[tuple[str, Callable]] = []
        if phase in ("premarket", "open"):
            strategies = [("A", self._check_strategy_a), ("B", self._check_strategy_b)]
        elif phase == "midday":
            strategies = [("B", self._check_strategy_b)]  # 돌파 지양
        else:
            strategies = [("A", self._check_strategy_a), ("B", self._check_strategy_b)]

        for strat_id, check_fn in strategies:
            triggered, stop_loss, reason = check_fn(history, current_price)
            if not triggered:
                continue

            # ── 연속 확인: N틱 연속 시그널이어야 실제 진입 ──
            self._entry_confirm[code] = self._entry_confirm.get(code, 0) + 1
            if self._entry_confirm[code] < ENTRY_CONFIRM_TICKS:
                return Signal(
                    type=SignalType.HOLD, code=code,
                    reason=f"[{strat_id}] 진입 확인 중 ({self._entry_confirm[code]}/{ENTRY_CONFIRM_TICKS}) — {reason}",
                )

            # 연속 확인 완료 → 실제 진입
            self._entry_confirm[code] = 0

            # 익절가 계산: R:R ≥ 2:1 보장
            risk = current_price - stop_loss
            if risk <= 0:
                continue
            take_profit = current_price + risk * self._risk.config.min_rr_ratio
            # 최소 TAKE_PROFIT_PCT 보장
            min_tp = current_price * (1 + TAKE_PROFIT_PCT / 100)
            take_profit = max(take_profit, min_tp)

            # R:R 검증
            rr_ok, rr_val = self._risk.check_rr_ratio(current_price, stop_loss, take_profit)
            if not rr_ok:
                continue

            # 수량 계산
            quantity = self._risk.calculate_position_size(current_price, stop_loss)
            if quantity <= 0:
                return Signal(type=SignalType.HOLD, code=code, reason="잔고 부족")

            # 매수 시점 기록 (최소 보유 기간 추적)
            self._hold_start[code] = self._cycle_count

            return Signal(
                type=SignalType.BUY,
                code=code,
                quantity=quantity,
                price=0,
                reason=f"[{strat_id}] {reason} | {filter_msg} | R:R {rr_val}:1",
                stop_loss=stop_loss,
                take_profit=take_profit,
                entry_strategy=strat_id,
            )

        # 전략 미충족 → 진입 카운터 리셋
        self._entry_confirm[code] = 0
        return Signal(
            type=SignalType.HOLD, code=code,
            reason=f"진입 조건 미충족 ({phase})",
        )
