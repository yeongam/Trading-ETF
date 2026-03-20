"""매매 스케줄러 - 전략 실행 및 주문 처리를 관리합니다."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .broker.base import BaseBroker, OrderResult
from .broker.toss import TossBroker
from .strategy.base import BaseStrategy, Signal, SignalType
from .risk import RiskManager
from .config import TradingConfig
from .screener.krx import fetch_candidates
from .screener.filter import screen, ScreenerConfig

logger = logging.getLogger(__name__)

_SCREEN_INTERVAL = 1800   # KO 스크리닝 간격 (30분)
MIN_DATA_NORMAL = 21      # 정상 최소 데이터 횟수 (EMA 20 최소 요구)


@dataclass
class StockInfo:
    """US 종목 추적 정보."""
    best_rank: int = 9999    # 최고 순위 (낮을수록 좋음, 0-indexed)
    in_chart: bool = True    # 현재 차트에 있는 종목


class TradingScheduler:
    """주기적으로 전략을 실행하고 매매 신호에 따라 주문을 처리합니다."""

    def __init__(
        self,
        broker: BaseBroker,
        strategy: BaseStrategy,
        config: TradingConfig,
        risk_manager: RiskManager | None = None,
        screener_config: ScreenerConfig | None = None,
    ) -> None:
        self._broker = broker
        self._strategy = strategy
        self._config = config
        self._risk = risk_manager
        self._screener_config = screener_config or ScreenerConfig()
        self._running = False
        self._trade_log: list[dict] = []
        self._first_trade_logged = False  # 최초 매매 시작 로그 플래그
        self._cycle_count: int = 0
        self._pre_existing_codes: set[str] = set()  # 시작 전 이미 보유 중인 종목 (매매 제외)
        self._last_sync_cycle: int = 0  # 마지막 포트폴리오 동기화 사이클
        self._last_daily_reset: str = ""  # 마지막 일일 리셋 날짜
        self._need_pre_existing_check: bool = False  # 시작 시 보유종목 감지 플래그
        # KO 스크리닝용
        self._cached_watchlist: list[str] = list(config.watchlist)
        self._last_screen_time: float = 0.0
        # US 종목 추적용
        self._stock_tracks: dict[str, StockInfo] = {}

    # ── 프로퍼티 ──────────────────────────────────────────────────────────────

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

    @property
    def total_pnl_amount(self) -> int:
        """trade_log에서 실시간 재계산 (누적 합산 오차 방지)."""
        return sum(t.get("pnl_amount", 0) for t in self._trade_log if t.get("success"))

    # ── US 종목 추적 ──────────────────────────────────────────────────────────

    def _update_stock_tracking(self, new_ranks: dict[str, int]) -> None:
        """캐시 갱신 시 US 종목 추적 정보 동기화."""
        # 1. 신규 진입 종목 추가 / 기존 종목 순위 업데이트
        for code, rank in new_ranks.items():
            if code not in self._stock_tracks:
                self._stock_tracks[code] = StockInfo(best_rank=rank)
                logger.info("신규 추적: %s (%d위)", code, rank + 1)
            else:
                info = self._stock_tracks[code]
                info.in_chart = True
                if rank < info.best_rank:
                    info.best_rank = rank

        # 2. 차트 이탈 종목 처리
        for code in list(self._stock_tracks.keys()):
            if code not in new_ranks:
                info = self._stock_tracks[code]
                if info.in_chart:
                    info.in_chart = False
                    data_count = self._strategy.get_data_count(code)
                    if data_count < MIN_DATA_NORMAL:
                        del self._stock_tracks[code]
                        logger.info("데이터 부족 → 추적 제거: %s (%d회)", code, data_count)
                    else:
                        logger.info("차트 이탈 (데이터 유지): %s (%d회)", code, data_count)

    def _get_effective_threshold(self) -> int:
        """유효 데이터 임계값 반환."""
        return MIN_DATA_NORMAL

    # ── KO 스크리닝 ───────────────────────────────────────────────────────────

    async def _get_watchlist_ko(self) -> list[str]:
        """KO 시장 감시 종목 반환 (pykrx 스크리닝)."""
        if self._config.watchlist:
            return self._config.watchlist
        now = time.time()
        if now - self._last_screen_time < _SCREEN_INTERVAL and self._cached_watchlist:
            return self._cached_watchlist
        logger.info("KO 종목 스크리닝 시작...")
        active_codes = set(self._risk.positions.keys()) if self._risk else set()
        candidates = fetch_candidates()
        self._cached_watchlist = screen(candidates, self._screener_config, exclude_codes=active_codes)
        self._last_screen_time = now
        logger.info("KO 스크리닝 완료: %s", self._cached_watchlist[:5])
        return self._cached_watchlist

    # ── 포트폴리오 동기화 ───────────────────────────────────────────────────

    SYNC_INTERVAL = 5  # 몇 사이클마다 동기화할지

    async def _sync_portfolio(self) -> None:
        """내부 포트폴리오의 각 종목을 개별 검증하여 유령 포지션 제거.

        /my 페이지 전체 스캔 대신 종목 상세 페이지에서 직접 보유 확인합니다.
        스캔 실패로 정상 포지션이 삭제되는 사고를 방지합니다.
        """
        if not self._risk or not isinstance(self._broker, TossBroker):
            return
        if not self._risk.positions:
            return

        # 내부 포지션 각각을 종목 페이지에서 개별 검증
        codes_to_check = [
            c for c in self._risk.positions.keys()
            if c not in self._pre_existing_codes
        ]
        if not codes_to_check:
            return

        logger.info("포트폴리오 동기화: %d개 종목 개별 검증 시작", len(codes_to_check))
        phantom: list[str] = []
        for code in codes_to_check:
            try:
                hold_info = await self._broker.verify_holding_on_page(code)
                if not hold_info.get("held"):
                    phantom.append(code)
            except Exception as e:
                # 검증 실패 시 안전하게 유지 (삭제하지 않음)
                logger.warning("보유 검증 실패 [%s] — 포지션 유지: %s", code, e)

        for code in phantom:
            pos = self._risk.positions.get(code)
            if pos:
                name = self._resolve_name(code)
                logger.warning(
                    "유령 포지션 제거: %s(%s) — 종목 페이지에서 보유 미확인 (진입가 %s원 × %d주)",
                    name, code, f"{pos.entry_price:,}", pos.quantity,
                )
                self._risk.close_position(code, pos.entry_price)  # 손익 0으로 처리

        if phantom:
            logger.info("포트폴리오 동기화 완료: 유령 %d개 제거", len(phantom))
        else:
            logger.info("포트폴리오 동기화 완료: 전체 %d개 정상 보유 확인", len(codes_to_check))

    # ── 주문 실행 ─────────────────────────────────────────────────────────────

    async def _execute_signal(self, signal: Signal) -> OrderResult | None:
        if signal.type == SignalType.HOLD:
            return None

        # 시작 전 이미 보유 중인 종목은 매매 제외
        if signal.code in self._pre_existing_codes:
            return None

        # 종목명 조회 (US: 캐시 즉시 반환, 네비게이션 없음) — 주문 전에 이름 확보
        price_info = await self._broker.get_price(signal.code)
        stock_name = price_info.name if price_info else signal.code
        mode_tag = "[모의]" if self._config.dry_run else "[실제]"

        # ETF 전용 모드: 매수 전 상세 페이지에서 ETF 2차 검증
        if (signal.type == SignalType.BUY
                and self._config.etf_only
                and isinstance(self._broker, TossBroker)):
            is_etf = await self._broker.verify_etf_on_detail_page(signal.code)
            if not is_etf:
                logger.info(
                    "ETF 전용: %s(%s) — 상세 페이지에서 ETF 미확인 → 매수 차단",
                    stock_name, signal.code,
                )
                return None

        if self._config.dry_run:
            logger.info("%s %s %s(%s) %d주 @ %s — %s",
                        mode_tag, signal.type.value, stock_name, signal.code,
                        signal.quantity, signal.price or "시장가", signal.reason)
            result = OrderResult(success=True, message=f"[DRY RUN] {signal.type.value}")
        elif signal.type == SignalType.BUY:
            logger.info("%s 매수 시도: %s(%s) %d주", mode_tag, stock_name, signal.code, signal.quantity)
            result = await self._broker.buy(signal.code, signal.quantity, signal.price)
        else:
            logger.info("%s 매도 시도: %s(%s) %d주", mode_tag, stock_name, signal.code, signal.quantity)
            result = await self._broker.sell(signal.code, signal.quantity, signal.price)

        if not result.success:
            logger.error("주문 실패: %s(%s) — %s", stock_name, signal.code, result.message)

        pnl_data: dict | None = None
        if result.success and self._risk and not self._config.dry_run:
            # 실제 주문 후 체결 검증
            if isinstance(self._broker, TossBroker):
                await asyncio.sleep(3)  # 체결 반영 대기

                if signal.type == SignalType.BUY:
                    # 종목 상세 페이지에서 직접 보유 확인 + 평균 매수가 추출
                    hold_info = await self._broker.verify_holding_on_page(signal.code)
                    if not hold_info.get("held"):
                        # 1차 실패 → 3초 후 재시도 (체결 지연 대응)
                        logger.warning(
                            "매수 체결 1차 미확인: %s(%s) — 3초 후 재확인",
                            stock_name, signal.code,
                        )
                        await asyncio.sleep(3)
                        hold_info = await self._broker.verify_holding_on_page(signal.code)

                    if not hold_info.get("held"):
                        logger.error(
                            "매수 체결 최종 미확인: %s(%s) — 보유 정보 없음 (포지션 등록 안함)",
                            stock_name, signal.code,
                        )
                        result = OrderResult(success=False, message="체결 미확인: 보유 정보 없음")
                    else:
                        # 진입가 결정 우선순위:
                        # 1. 토스 페이지의 평균 매수가 (가장 정확)
                        # 2. 상세 페이지 현재가
                        # 3. 캐시 현재가 (최후 수단)
                        avg_price = hold_info.get("avg_price", 0)
                        page_price = hold_info.get("page_price", 0)
                        cache_price = price_info.current_price if price_info else signal.price

                        if avg_price > 0:
                            entry_price = avg_price
                            price_src = "평균매수가"
                        elif page_price > 0:
                            entry_price = page_price
                            price_src = "페이지현재가"
                        else:
                            entry_price = cache_price
                            price_src = "캐시가격"

                        if entry_price > 0:
                            self._risk.open_position(
                                signal.code, entry_price, signal.quantity,
                                stop_loss=signal.stop_loss,
                                take_profit=signal.take_profit,
                                entry_strategy=signal.entry_strategy,
                            )
                            logger.info(
                                "매수 체결 확인 → 포지션 등록: %s(%s) %d주 @ %s원 (%s)",
                                stock_name, signal.code, signal.quantity,
                                f"{entry_price:,}", price_src,
                            )
                        else:
                            logger.warning("가격 미확인 — 리스크 추적 건너뜀: %s", signal.code)

                elif signal.type == SignalType.SELL:
                    # 매도: 상세 페이지에서 실제 현재가 확인 (캐시 가격 오차 방지)
                    sell_info = await self._broker.verify_holding_on_page(signal.code)
                    page_price = sell_info.get("page_price", 0)
                    cache_price = price_info.current_price if price_info else signal.price
                    current_price = page_price if page_price > 0 else cache_price
                    if page_price > 0 and cache_price > 0 and abs(page_price - cache_price) / cache_price > 0.1:
                        logger.warning(
                            "매도 가격 차이: 페이지 %s원 vs 캐시 %s원 (페이지 가격 사용)",
                            f"{page_price:,}", f"{cache_price:,}",
                        )
                    if current_price > 0:
                        pnl_data = self._risk.close_position(signal.code, current_price)
                        if pnl_data:
                            logger.info(
                                "매도 체결 → 포지션 청산: %s(%s) 수익 %+d원 (%.2f%%)",
                                stock_name, signal.code,
                                pnl_data["pnl_amount"], pnl_data["pnl_pct"],
                            )

        elif result.success and self._risk and self._config.dry_run:
            current_price = price_info.current_price if price_info else signal.price
            if current_price <= 0:
                logger.warning("가격 미확인 — 리스크 추적 건너뜀: %s", signal.code)
            elif signal.type == SignalType.BUY:
                self._risk.open_position(
                    signal.code, current_price, signal.quantity,
                    stop_loss=signal.stop_loss,
                    take_profit=signal.take_profit,
                    entry_strategy=signal.entry_strategy,
                )
            elif signal.type == SignalType.SELL:
                pnl_data = self._risk.close_position(signal.code, current_price)

        self._trade_log.append({
            "timestamp": datetime.now().isoformat(),
            "type": signal.type.value,
            "code": signal.code,
            "name": stock_name,
            "quantity": signal.quantity,
            "price": signal.price,
            "reason": signal.reason,
            "dry_run": self._config.dry_run,
            "success": result.success,
            "message": result.message,
            "pnl_amount": pnl_data.get("pnl_amount", 0) if pnl_data else 0,
        })
        return result

    # ── 메인 루프 ─────────────────────────────────────────────────────────────

    def _resolve_name(self, code: str) -> str:
        """브로커 캐시에서 종목명 조회."""
        if isinstance(self._broker, TossBroker) and code in self._broker._us_price_cache:
            return self._broker._us_price_cache[code].name
        return code

    def _resolve_current_price(self, code: str) -> int:
        """브로커 캐시에서 현재가 조회."""
        if isinstance(self._broker, TossBroker) and code in self._broker._us_price_cache:
            return self._broker._us_price_cache[code].current_price
        return 0

    def _log_portfolio(self, with_current_price: bool = False) -> None:
        """현재 포트폴리오 현황을 로그로 출력합니다."""
        if not self._risk:
            return
        stats = self._risk.stats
        positions = self._risk.positions
        budget = stats.get("total_budget", 0)
        invested = stats.get("total_invested", 0)
        remaining = stats.get("remaining_budget", 0)
        mode = "실제매매" if not self._config.dry_run else "모의매매"

        logger.info(
            "── 포트폴리오 [%s] ── 보유 %d종목 | 투자 %s원 / 예산 %s원 (잔여 %s원) | 총손익 %+d원",
            mode, len(positions), f"{invested:,}", f"{budget:,}", f"{remaining:,}",
            self.total_pnl_amount,
        )
        if positions:
            total_unrealized = 0
            for code, pos in positions.items():
                name = self._resolve_name(code)
                weight = (pos.entry_price * pos.quantity) / budget * 100 if budget > 0 else 0
                if with_current_price:
                    cur = self._resolve_current_price(code)
                    if cur > 0 and pos.entry_price > 0:
                        cur_pnl_pct = (cur - pos.entry_price) / pos.entry_price * 100
                        cur_pnl_amt = (cur - pos.entry_price) * pos.quantity
                        total_unrealized += cur_pnl_amt
                        logger.info(
                            "  ├ %s(%s): %d주 @ %s원 → 현재 %s원 (%+.2f%% | %+d원) 비중 %.1f%%",
                            name, code, pos.quantity, f"{pos.entry_price:,}",
                            f"{cur:,}", cur_pnl_pct, int(cur_pnl_amt), weight,
                        )
                    else:
                        logger.info(
                            "  ├ %s(%s): %d주 @ %s원 (비중 %.1f%%)",
                            name, code, pos.quantity, f"{pos.entry_price:,}", weight,
                        )
                else:
                    pnl_pct = (pos.highest_price - pos.entry_price) / pos.entry_price * 100 if pos.entry_price > 0 else 0
                    logger.info(
                        "  ├ %s(%s): %d주 @ %s원 (비중 %.1f%% | 최고 %+.1f%%)",
                        name, code, pos.quantity, f"{pos.entry_price:,}", weight, pnl_pct,
                    )
            if with_current_price and total_unrealized != 0:
                logger.info("  └ 미실현 손익 합계: %+d원", int(total_unrealized))

    async def run_once(self) -> list[dict]:
        """감시 종목에 대해 전략을 1회 실행합니다."""
        self._cycle_count += 1
        results = []

        # 일일 리셋 (US 동부시간 기준 날짜 변경 시 데일리 손익/쿨다운 초기화)
        et_today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        if self._risk and self._last_daily_reset != et_today:
            self._last_daily_reset = et_today
            self._risk.reset_daily()
            logger.info("일일 리셋 완료 (데일리 손익/쿨다운 초기화) [ET: %s]", et_today)

        if self._config.market == "us" and isinstance(self._broker, TossBroker):
            # US: 캐시 갱신 후 추적 업데이트 + 메인 페이지 세션 유지
            refreshed = await self._broker.refresh_us_cache_if_needed()
            if refreshed:
                self._update_stock_tracking(self._broker.get_us_ranks())
                # 메인 페이지 keep-alive (차트 탭만 활동하므로 메인 탭 세션 만료 방지)
                await self._broker.keep_alive()

                # 첫 캐시 갱신 후 기존 보유 종목 감지 (차트 탭 사용 → 메인 페이지 영향 없음)
                if self._need_pre_existing_check:
                    self._need_pre_existing_check = False
                    held = await self._broker.get_held_codes_via_chart_tab()
                    if held:
                        self._pre_existing_codes = held
                        names = [self._resolve_name(c) for c in held]
                        logger.info("기존 보유 종목 %d개 (매매 제외): %s",
                                    len(held), ", ".join(names))
                    else:
                        logger.info("기존 보유 종목 없음 — 모든 종목 매매 가능")
                # 데이터 수집 후 5초 대기 → 보유 종목 현재가 포트폴리오 출력
                if self._risk and self._risk.positions:
                    await asyncio.sleep(5)
                    self._log_portfolio(with_current_price=True)

            if not self._stock_tracks:
                logger.warning("추적 종목 없음 — 캐시 로드 대기 중")
                return results

            threshold = self._get_effective_threshold()
            # 차트에 있는 종목 + 보유 중인 종목(차트 이탈 후에도 청산 관리 필요)만 처리
            # 차트 이탈 + 포지션 없는 종목은 시세 조회 불가 → 매 사이클 "시세 조회 실패" 방지
            active_pos = set(self._risk.positions.keys()) if self._risk else set()
            codes = [
                c for c, i in self._stock_tracks.items()
                if i.in_chart or c in active_pos
            ]

            # ETF 전용 모드: 개별주 매수 차단 (보유 중인 개별주는 청산 관리 위해 유지)
            if self._config.etf_only and isinstance(self._broker, TossBroker):
                etf_codes = []
                non_etf_skipped = []
                for c in codes:
                    sp = self._broker._us_price_cache.get(c)
                    if sp and sp.is_etf:
                        etf_codes.append(c)
                    elif c in active_pos:
                        # 이미 보유 중인 개별주는 청산 관리를 위해 유지
                        etf_codes.append(c)
                    else:
                        non_etf_skipped.append(self._resolve_name(c))
                if non_etf_skipped:
                    logger.debug("ETF 전용: 개별주 %d개 스킵 (%s)", len(non_etf_skipped), ", ".join(non_etf_skipped[:5]))
                codes = etf_codes
        else:
            # KO: pykrx 스크리닝
            threshold = MIN_DATA_NORMAL
            codes = await self._get_watchlist_ko()
            if not codes:
                logger.warning("감시 종목 없음 (스크리닝 결과 0개)")
                return results

        # 데이터 수집 현황 요약
        collecting = 0
        ready = 0
        stock_data: list[tuple[str, str, int]] = []  # (name, code, count)
        for code in codes:
            count = self._strategy.get_data_count(code)
            name = self._resolve_name(code)
            stock_data.append((name, code, count))
            if count < threshold:
                collecting += 1
            else:
                ready += 1
        invested = self._risk.stats.get("total_invested", 0) if self._risk else 0
        budget = self._risk.stats.get("total_budget", 0) if self._risk else 0
        logger.info(
            "── 사이클 #%d ── %d종목 (분석가능 %d / 수집중 %d) | 투자 %s원 / 예산 %s원 | 주기 %d초",
            self._cycle_count, len(codes), ready, collecting,
            f"{invested:,}", f"{budget:,}", self._config.check_interval,
        )
        for i, (name, code, count) in enumerate(stock_data, 1):
            status = "✔" if count >= threshold else f"{count}/{threshold}"
            logger.info("  %2d. %-20s %s", i, name, status)

        # 전략 사이클 카운터 진행 (종목 루프 전에 1회만 호출 — 종목 수에 영향받지 않음)
        self._strategy.advance_cycle()

        had_trade = False
        for code in codes:
            try:
                signal = await self._strategy.analyze(code, self._broker, min_data=threshold)
                if signal.type == SignalType.HOLD and signal.reason:
                    name = self._resolve_name(code)
                    logger.info("  ↳ %s: %s", name, signal.reason)
                result = await self._execute_signal(signal)
                if result is not None:
                    had_trade = True
                results.append({
                    "code": code,
                    "signal": signal.type.value,
                    "reason": signal.reason,
                    "executed": result is not None,
                })
            except Exception as e:
                logger.error("전략 실행 오류 [%s]: %s", code, e)
                results.append({"code": code, "signal": "error", "reason": str(e)})

        # 쿨다운 소모 (사이클당 1회)
        if self._risk:
            self._risk.consume_cooldown()

        # 포트폴리오 동기화 (N 사이클마다 실제 보유 종목과 대조)
        if (self._risk and self._risk.positions
                and self._cycle_count - self._last_sync_cycle >= self.SYNC_INTERVAL):
            self._last_sync_cycle = self._cycle_count
            await self._sync_portfolio()

        # 최초 매매 발생 시 알림
        if had_trade and not self._first_trade_logged:
            self._first_trade_logged = True
            logger.info("★★★ 최초 매매 실행! 데이터 수집 완료 후 실제 매매가 시작되었습니다. ★★★")

        # 포트폴리오 현황 출력
        self._log_portfolio()

        return results

    async def start(self) -> None:
        """스케줄러를 시작합니다. check_interval 간격으로 전략을 반복 실행합니다."""
        self._running = True
        try:
            logger.info("스케줄러 시작 (전략: %s, 간격: %d초)", self._strategy.name, self._config.check_interval)

            logger.info("토스증권 로그인 시도...")
            logged_in = await self._broker.login()
            if not logged_in:
                logger.error("로그인 실패. 스케줄러를 중지합니다.")
                return

            # 시작 전 이미 보유 중인 종목 기록 (매매 제외 대상)
            # 첫 run_once에서 US 캐시 갱신 후 자동 감지 (메인 페이지 이동 없음)
            self._need_pre_existing_check = True

            await self.run_once()

            while self._running:
                await asyncio.sleep(self._config.check_interval)
                await self.run_once()
        except Exception as e:
            logger.error("스케줄러 오류로 중지: %s", e, exc_info=True)
        finally:
            self._running = False

    def stop(self) -> None:
        """스케줄러를 중지합니다."""
        self._running = False
        logger.info("스케줄러 중지")
