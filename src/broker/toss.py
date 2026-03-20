"""토스증권 Playwright 기반 브로커 구현.

토스증권은 공식 API가 없으므로 웹 브라우저 자동화로 거래를 수행합니다.
로그인은 토스 앱 인증(QR 또는 알림)을 사용하므로 최초 1회 수동 인증이 필요합니다.

=== 셀렉터 튜닝 가이드 ===
실제 토스증권 DOM과 다를 수 있는 셀렉터에는 [TUNE] 주석이 붙어 있습니다.
브라우저 DevTools(F12)에서 해당 요소를 우클릭 > Copy > Copy selector로
실제 셀렉터를 확인한 뒤 교체하세요.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import sys
import time
from pathlib import Path
from playwright.async_api import async_playwright, Browser, Page, Playwright

from .base import BaseBroker, StockPrice, OrderResult, Position
from ..config import TossConfig

logger = logging.getLogger(__name__)

# ============================================================
# [TUNE] 셀렉터 상수 - 실제 DOM에 맞게 이 부분만 수정하면 됩니다
# ============================================================

# --- 로그인 ---
# 로그인 완료 후 메인 페이지에 나타나는 요소
SEL_LOGIN_COMPLETE = 'a[href="/my"], [data-testid="gnb-my"]'  # [TUNE] 상단 GNB의 'MY' 링크

# --- MY 페이지 ---
# get_held_codes_via_chart_tab()에서 JS evaluate로 직접 처리 (셀렉터 미사용)

# --- 종목 상세 페이지 ---
# [TUNE] 현재가: 메이우 테크놀로지 페이지에서 확인된 셀렉터
# div._1sivumi0._1sivumi2 > div:nth-child(2) > span._1sivumi3 (원화/달러 표시 영역)
SEL_STOCK_PRICE = (
    'div._1sivumi0._1sivumi2 > div:nth-child(2) > span._1sivumi3, '
    '[class*="_1sivumi2"] div:nth-child(2) span[class*="_1sivumi3"], '
    '[class*="_1sivumi2"] span[class*="_1sivumi3"]'
)
SEL_STOCK_NAME = 'h1, [class*="stock-name"], [class*="title"] > span'  # [TUNE] 종목명
SEL_STOCK_CHANGE = '[class*="change-rate"], [class*="rate"]'  # [TUNE] 등락률 (+2.35%)
SEL_STOCK_VOLUME = '[class*="volume"], td:has(+ td:has-text("거래량"))'  # [TUNE] 거래량

# --- 주문 UI ---
# 탭/시장가/제출 버튼: _place_order 내에서 JS evaluate로 직접 클릭 (셀렉터 호환성 문제 회피)
# 수량 입력 (div 기반 커스텀 컴포넌트) — [TUNE] 필요 시 교체
SEL_ORDER_QTY_INPUT = '#trade-order-section > div:nth-child(2) > div > div > div:nth-child(2) > div._13izhfo2 > div'
SEL_ORDER_PRICE_INPUT = 'input[placeholder*="가격"], input[class*="price"]'  # [TUNE] 지정가 입력


def _parse_number(text: str) -> int:
    """'1,234원' 같은 텍스트에서 숫자만 추출."""
    cleaned = re.sub(r"[^\d]", "", text)
    return int(cleaned) if cleaned else 0


def _parse_rate(text: str) -> float:
    """'+2.35%' 같은 텍스트에서 등락률 추출."""
    match = re.search(r"[+-]?\d+\.?\d*", text)
    return float(match.group()) if match else 0.0


class TossBroker(BaseBroker):
    """토스증권 웹 자동화 브로커."""

    def __init__(self, config: TossConfig) -> None:
        self._config = config
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._page: Page | None = None
        self._chart_page: Page | None = None  # 차트 전용 탭 (메인 탭과 분리)
        self._us_price_cache: dict[str, StockPrice] = {}
        self._us_price_cache_time: float = 0.0
        self._us_rank_cache: dict[str, int] = {}  # 종목코드 → 순위(0-indexed)
        self._fx_rate: float = 1500.0  # 달러 환율 (첫 캐시 갱신 시 실시간 값으로 교체)
        self._etf_verified: dict[str, bool] = {}  # ETF 상세 검증 캐시 (종목당 1회)

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("브라우저가 시작되지 않았습니다. login()을 먼저 호출하세요.")
        if self._page.is_closed():
            raise RuntimeError("브라우저 페이지가 닫혔습니다. 프로그램을 재시작해주세요.")
        return self._page

    async def _launch_browser(self) -> Page:
        # 이전 인스턴스 정리 (재시작 시 잔류 프로세스 충돌 방지)
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._page = None
        self._chart_page = None

        self._pw = await async_playwright().start()
        launch_kwargs: dict = dict(
            headless=self._config.headless,
            slow_mo=self._config.slow_mo,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        # macOS에 Chrome이 설치된 경우 channel="chrome" 사용 (독 아이콘 + 전면 표시 지원)
        # Chrome이 없으면 Playwright 번들 Chromium으로 폴백
        chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if sys.platform == "darwin" and Path(chrome_path).exists():
            launch_kwargs["channel"] = "chrome"
            logger.info("시스템 Chrome 사용: %s", chrome_path)
        else:
            logger.info("Playwright 번들 Chromium 사용")
        self._browser = await self._pw.chromium.launch(**launch_kwargs)
        context = await self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="ko-KR",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        # 자동화 감지 우회: navigator.webdriver 숨기기
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)
        self._page = await context.new_page()
        self._page.set_default_timeout(self._config.timeout)
        # _chart_page는 로그인 완료 후 첫 사용 시 생성 (지연 초기화)
        return self._page

    async def login(self) -> bool:
        """토스증권 로그인.

        토스 앱 인증이 필요하므로 headless=False 상태에서
        사용자가 직접 QR/알림 인증을 완료해야 합니다.
        """
        page = await self._launch_browser()
        logger.info("토스증권 로그인 페이지로 이동합니다...")
        await page.goto(
            f"{self._config.login_url}/signin?redirectUrl=%2F",
            wait_until="domcontentloaded",
        )
        await page.bring_to_front()  # macOS에서 창을 전면으로 강제 표시
        # macOS: osascript로 Chrome을 독/화면 전면에 강제 표시
        if sys.platform == "darwin":
            subprocess.Popen(
                ["osascript", "-e", 'tell application "Google Chrome" to activate'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        logger.info("토스 앱에서 로그인을 승인해주세요... (최대 2분 대기)")
        try:
            # 로그인 성공 시 URL이 /signin에서 벗어남
            await page.wait_for_url(
                lambda url: "/signin" not in url,
                timeout=120000,
            )
            logger.info("로그인 성공! (현재 URL: %s)", page.url)
            return True
        except Exception as e:
            logger.error("로그인 실패 (2분 내 인증 없음): %s", e)
            return False

    async def get_balance(self) -> int:
        """예수금 조회 (미사용 — BaseBroker 인터페이스 충족용)."""
        return 0

    async def get_positions(self) -> list[Position]:
        """보유 종목 조회 (미사용 — BaseBroker 인터페이스 충족용)."""
        return []

    async def verify_etf_on_detail_page(self, code: str) -> bool:
        """종목 상세 페이지에서 'ETF' 텍스트 존재 여부로 ETF 2차 검증.

        차트 탭에서 종목명만으로 1차 판별 후, 매수 직전에 상세 페이지에서 확인합니다.
        """
        # 캐시 히트: 이미 검증된 종목은 재방문 불필요
        if code in self._etf_verified:
            return self._etf_verified[code]

        try:
            page = await self._get_chart_page()
            await page.goto(
                f"{self._config.login_url}/stocks/{code}",
                wait_until="domcontentloaded",
            )
            await asyncio.sleep(3)
            is_etf = await page.evaluate("""() => {
                const text = document.body.innerText;
                // "ETF" 키워드가 페이지 내 존재하는지 확인
                return /ETF/i.test(text);
            }""")
            self._etf_verified[code] = bool(is_etf)
            logger.info("ETF 상세 검증 [%s]: %s (캐싱됨)", code, "ETF" if is_etf else "개별주")

            # 차트 페이지로 복귀
            chart_url = f"{self._config.login_url}/?market=us&live-chart=biggest_total_amount"
            await page.goto(chart_url, wait_until="domcontentloaded")

            return bool(is_etf)
        except Exception as e:
            logger.warning("ETF 상세 검증 실패 [%s]: %s — 1차 판별 유지", code, e)
            return False

    async def verify_holding_on_page(self, code: str) -> dict:
        """종목 상세 페이지에서 보유 여부 + 평균 매수가 + 현재가를 확인 (차트 탭 사용).

        Returns:
            dict: {held: bool, qty: int, avg_price: int, page_price: int, method: str}
            - avg_price: 토스가 표시하는 평균 매수가 (KRW). 추출 실패 시 0.
            - page_price: 상세 페이지의 현재가 (KRW). 추출 실패 시 0.
        """
        try:
            page = await self._get_chart_page()
            await page.goto(
                f"{self._config.login_url}/stocks/{code}",
                wait_until="domcontentloaded",
            )
            await asyncio.sleep(4)  # React 렌더링 + 보유 정보 로딩 대기

            result = await page.evaluate("""() => {
                // ── 현재가 추출 (페이지 상단) ──
                let pagePrice = 0;
                const priceEl = document.querySelector(
                    'div._1sivumi0._1sivumi2 > div:nth-child(2) > span._1sivumi3, ' +
                    '[class*="_1sivumi2"] span[class*="_1sivumi3"]'
                );
                if (priceEl) {
                    const pt = priceEl.innerText.replace(/[^\\d]/g, '');
                    if (pt) pagePrice = parseInt(pt);
                }

                // ── 평균 매수가 추출 ──
                let avgPrice = 0;
                const allText = document.body.innerText;
                // "평균 매수가 12,345원" 또는 "평균매수가 $12.34" 등
                const avgPatterns = [
                    /평균\\s*매수가\\s*\\$?([\\d,.]+)/,
                    /매수\\s*평균가\\s*\\$?([\\d,.]+)/,
                    /평균매수가\\s*\\$?([\\d,.]+)/,
                    /매수\\s*평균\\s*\\$?([\\d,.]+)/,
                ];
                for (const pat of avgPatterns) {
                    const am = allText.match(pat);
                    if (am) {
                        const raw = am[1].replace(/,/g, '');
                        // 달러 표시인지 확인 (소수점 포함 + 값이 작으면 달러)
                        const val = parseFloat(raw);
                        if (am[0].includes('$') || (val < 1000 && raw.includes('.'))) {
                            // 달러 → 원화 변환은 Python에서 처리 (여기선 달러 플래그만)
                            avgPrice = -val;  // 음수 = 달러 표시
                        } else {
                            avgPrice = parseInt(raw);
                        }
                        break;
                    }
                }

                // ── 1순위: 주문 영역(#trade-order-section) 내에서 확인 ──
                const section = document.querySelector('#trade-order-section');
                if (section) {
                    const sText = section.innerText;
                    const qtyMatch = sText.match(/보유\\s*(?:수량)?\\s*(\\d+)/);
                    if (qtyMatch && parseInt(qtyMatch[1]) > 0)
                        return {held: true, m: '주문영역보유', qty: parseInt(qtyMatch[1]),
                                avgPrice: avgPrice, pagePrice: pagePrice};
                    const sellMatch = sText.match(/판매\\s*가능\\s*(\\d+)/);
                    if (sellMatch && parseInt(sellMatch[1]) > 0)
                        return {held: true, m: '판매가능', qty: parseInt(sellMatch[1]),
                                avgPrice: avgPrice, pagePrice: pagePrice};
                }

                // ── 2순위: 내 보유 현황 섹션 ──
                const personalPatterns = [
                    /내\\s*보유\\s*현황/,
                    /내\\s*투자/,
                    /나의\\s*보유/,
                    /평균\\s*매수가|매수\\s*평균가|평균매수가/,
                ];
                for (const pat of personalPatterns) {
                    if (pat.test(allText))
                        return {held: true, m: pat.source, qty: -1,
                                avgPrice: avgPrice, pagePrice: pagePrice};
                }

                // ── 3순위: 판매하기 버튼 활성화 여부 ──
                if (section) {
                    const btns = section.querySelectorAll('button');
                    for (const btn of btns) {
                        const t = btn.innerText.trim();
                        if (t.includes('판매') && t.includes('하기') && !btn.disabled)
                            return {held: true, m: '판매하기활성', qty: -1,
                                    avgPrice: avgPrice, pagePrice: pagePrice};
                    }
                }

                return {held: false, m: null, qty: 0, avgPrice: 0, pagePrice: pagePrice};
            }""")

            is_held = result.get("held", False)
            method = result.get("m")
            avg_price_raw = result.get("avgPrice", 0)
            page_price = result.get("pagePrice", 0)

            # 평균 매수가: 달러 표시(음수)면 환율 변환
            avg_price = 0
            if avg_price_raw < 0:
                # 달러 → 원화
                avg_price = int(abs(avg_price_raw) * self._fx_rate)
                logger.info("평균 매수가 (달러→원): $%.2f × %.0f = %s원",
                            abs(avg_price_raw), self._fx_rate, f"{avg_price:,}")
            elif avg_price_raw > 0:
                avg_price = int(avg_price_raw)

            if is_held:
                logger.info(
                    "보유 확인 [%s]: %s (수량: %s, 평균매수가: %s원, 페이지현재가: %s원)",
                    code, method, result.get("qty"),
                    f"{avg_price:,}" if avg_price > 0 else "미확인",
                    f"{page_price:,}" if page_price > 0 else "미확인",
                )
            else:
                debug_text = await page.evaluate("""() => {
                    const section = document.querySelector('#trade-order-section');
                    const sText = section ? section.innerText.substring(0, 500) : '(주문영역 없음)';
                    const body = document.body.innerText;
                    const idx = body.indexOf('보유');
                    const holdContext = idx >= 0
                        ? body.substring(Math.max(0, idx - 30), idx + 50)
                        : '(보유 키워드 없음)';
                    return { orderSection: sText, holdContext: holdContext };
                }""")
                logger.warning(
                    "보유 미감지 [%s]: 주문영역=[%s] / 보유컨텍스트=[%s]",
                    code,
                    debug_text.get("orderSection", "?")[:200],
                    debug_text.get("holdContext", "?"),
                )

            # 차트 탭을 원래 차트 페이지로 복귀
            chart_url = f"{self._config.login_url}/?market=us&live-chart=biggest_total_amount"
            await page.goto(chart_url, wait_until="domcontentloaded")

            return {
                "held": is_held,
                "avg_price": avg_price,
                "page_price": page_price,
                "qty": result.get("qty", 0),
            }
        except Exception as e:
            logger.warning("보유 확인 실패 [%s]: %s", code, e)
            return {"held": False, "avg_price": 0, "page_price": 0, "qty": 0}

    async def get_held_codes_via_chart_tab(self) -> set[str]:
        """차트 탭을 이용해 보유 종목 코드만 추출 (메인 탭 이동 없음).

        /my 페이지에서 보유종목 영역의 주식 링크만 추출합니다.
        재시도 + 다단계 셀렉터로 안정성을 확보합니다.
        """
        try:
            page = await self._get_chart_page()
            await page.goto(f"{self._config.login_url}/my", wait_until="domcontentloaded")

            # networkidle 대기 — 보유종목 비동기 로딩 완료 대기
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            await asyncio.sleep(5)  # 추가 렌더링 대기 (3→5초)

            codes = await page.evaluate("""() => {
                const result = [];
                const seen = new Set();

                // ── 1단계: 보유종목 섹션 특정 ──
                // 토스 MY 페이지 구조: "보유 종목" 또는 "투자" 헤더 아래 목록
                let container = null;

                // 방법 A: "보유" 텍스트가 포함된 헤더 → 부모/형제에서 링크 탐색
                const headers = document.querySelectorAll('h2, h3, h4, [class*="header"], [class*="title"]');
                for (const h of headers) {
                    const t = h.innerText.trim();
                    if (t.includes('보유') || t.includes('투자') || t.includes('주식')) {
                        // 헤더의 부모(섹션) 또는 다음 형제에서 탐색
                        container = h.closest('section') || h.parentElement;
                        break;
                    }
                }

                // 방법 B: 클래스명 기반 (기존 방식 유지)
                if (!container) {
                    container = document.querySelector(
                        '[class*="holding"], [class*="stock-list"], [class*="portfolio"], ' +
                        '[class*="asset"], [class*="invest"]'
                    );
                }

                // 방법 C: main 영역
                if (!container) container = document.querySelector('main');

                // 방법 D: 전체 바디 (최후 수단)
                if (!container) container = document.body;

                const links = container.querySelectorAll('a[href*="/stocks/"]');
                for (const link of links) {
                    const m = link.href.match(/\\/stocks\\/(\\w+)/);
                    if (m && !seen.has(m[1])) {
                        seen.add(m[1]);
                        result.push(m[1]);
                    }
                }

                // 디버그: 어떤 컨테이너가 선택됐는지 + 페이지 구조 정보
                const containerInfo = container === document.body ? 'body(fallback)'
                    : (container.tagName + '.' + (container.className || '').substring(0, 50));

                return { codes: result, container: containerInfo, linkCount: links.length };
            }""")

            # 결과 파싱
            found_codes = codes.get("codes", []) if isinstance(codes, dict) else codes
            container_info = codes.get("container", "?") if isinstance(codes, dict) else "?"
            link_count = codes.get("linkCount", 0) if isinstance(codes, dict) else len(found_codes)

            logger.info(
                "보유종목 스캔: %d개 감지 (컨테이너: %s, 링크: %d개)",
                len(found_codes), container_info, link_count,
            )

            # 결과가 비어있으면 1회 재시도 (페이지 로딩 지연)
            if not found_codes:
                logger.info("보유종목 0개 — 3초 후 재스캔")
                await asyncio.sleep(3)
                retry_codes = await page.evaluate("""() => {
                    const result = [];
                    const seen = new Set();
                    const links = document.querySelectorAll('a[href*="/stocks/"]');
                    for (const link of links) {
                        const m = link.href.match(/\\/stocks\\/(\\w+)/);
                        if (m && !seen.has(m[1])) {
                            seen.add(m[1]);
                            result.push(m[1]);
                        }
                    }
                    return result;
                }""")
                if retry_codes:
                    logger.info("재스캔 결과: %d개 종목 감지", len(retry_codes))
                    found_codes = retry_codes

            chart_url = f"{self._config.login_url}/?market=us&live-chart=biggest_total_amount"
            await page.goto(chart_url, wait_until="domcontentloaded")

            return set(found_codes) if found_codes else set()
        except Exception as e:
            logger.warning("보유종목 코드 조회 실패: %s", e)
            return set()

    async def get_price(self, code: str) -> StockPrice | None:
        """종목 현재가 조회."""
        if code.startswith("US"):
            # 캐시가 60초 이상 지났으면 차트에서 전체 갱신
            if time.time() - self._us_price_cache_time > 60 or not self._us_price_cache:
                await self._refresh_us_price_cache()
            return self._us_price_cache.get(code)

        page = self.page
        await page.goto(f"{self._config.login_url}/stocks/{code}", wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        try:
            await page.wait_for_selector(SEL_STOCK_PRICE, timeout=20000)
            price_el = await page.query_selector(SEL_STOCK_PRICE)
            name_el = await page.query_selector(SEL_STOCK_NAME)
            price_text = await price_el.inner_text() if price_el else "0"
            name_text = await name_el.inner_text() if name_el else code
            return StockPrice(code=code, name=name_text.strip(),
                              current_price=_parse_number(price_text),
                              change_rate=0.0, volume=0)
        except Exception as e:
            logger.error("시세 조회 실패 [%s]: %s", code, e)
            return None

    async def _get_chart_page(self) -> Page:
        """차트 전용 탭 반환. 없거나 닫혀 있으면 로그인된 컨텍스트에서 새로 생성."""
        if self._chart_page is None or self._chart_page.is_closed():
            context = self.page.context
            self._chart_page = await context.new_page()
            self._chart_page.set_default_timeout(self._config.timeout)
            logger.info("차트 전용 탭 생성 완료")
        return self._chart_page

    async def _refresh_us_price_cache(self) -> None:
        """차트 페이지에서 US 종목 가격 전체를 한 번에 수집해 캐시."""
        # 차트 전용 탭 — 로그인 후 처음 호출 시 생성 (메인 탭과 간섭 없음)
        page = await self._get_chart_page()
        chart_url = f"{self._config.login_url}/?market=us&live-chart=biggest_total_amount"

        # 항상 차트 페이지로 이동 (URL 체크 제거 — 에러 페이지 방치 방지)
        for attempt in range(3):
            try:
                await page.goto(chart_url, wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                await page.wait_for_selector('a[href*="/stocks/US"]', timeout=30000)
                break  # 셀렉터 발견 시 루프 탈출
            except Exception as e:
                if attempt < 2:
                    logger.warning("US 차트 로드 재시도 중 (%d/3): %s", attempt + 1, e)
                    await asyncio.sleep(5)
                else:
                    logger.error("US 차트 로드 실패 (3회 시도): %s", e)
                    return

        rows = await page.evaluate("""() => {
            // 페이지에서 실시간 환율 추출 ("달러 환율 1,506.30" 등)
            const pageText = document.body.innerText;
            const fxMatch = pageText.match(/달러\\s*환율\\s*([\\d,]+\\.?\\d*)/);
            const fxRate = fxMatch ? parseFloat(fxMatch[1].replace(/,/g, '')) : 0;

            const result = [];
            const seen = new Set();
            const links = document.querySelectorAll('a[href*="/stocks/US"]');
            for (const link of links) {
                const m = link.href.match(/\\/stocks\\/(US\\w+)/);
                if (!m || seen.has(m[1])) continue;
                seen.add(m[1]);

                const container = link.closest('li') ?? link.parentElement?.parentElement;
                if (!container) continue;

                const text = container.innerText;
                // US 종목: 달러 가격을 우선 사용 (원화 패턴은 오탐 위험)
                // 핵심: 변동금액(-$2.35)이 현재가($25.50)보다 먼저 나올 수 있으므로
                // 모든 달러 값 중 가장 큰 값을 현재가로 사용 (변동금액 < 현재가)
                const dollarAll = [...text.matchAll(/\\$([\\d,]+\\.?\\d*)/g)];
                let dollarMatch = null;
                let maxVal = 0;
                for (const dm of dollarAll) {
                    const v = parseFloat(dm[1].replace(/,/g, ''));
                    if (v > maxVal) { maxVal = v; dollarMatch = dm; }
                }
                const changeMatch = text.match(/[+-]?[\\d.]+%/);
                // 거래량 추출: "123만", "1.2억", "45,678" 등
                const volMatch = text.match(/([\\d,]+\\.?\\d*)\\s*(만|억)/);
                let vol = 0;
                if (volMatch) {
                    const num = parseFloat(volMatch[1].replace(/,/g, ''));
                    if (volMatch[2] === '억') vol = Math.round(num * 100000000);
                    else if (volMatch[2] === '만') vol = Math.round(num * 10000);
                } else {
                    const plainVol = text.match(/거래량\\s*([\\d,]+)/);
                    if (plainVol) vol = parseInt(plainVol[1].replace(/,/g, ''), 10);
                }
                const lines = text.split('\\n')
                    .map(l => l.trim())
                    .filter(l => l && !/^\\d+$/.test(l) && !l.startsWith('$')
                                  && !l.includes('%') && !l.includes('억'));

                const stockName = lines[0] || m[1];
                // ETF 판별: 토스에서 ETF는 종목명이 영문 티커만 표시 (한글 없음)
                // 개별주는 "AST 스페이스모바일"처럼 한글+영어 혼합
                const hasKorean = /[가-힣]/.test(stockName);
                result.push({
                    code: m[1],
                    priceRaw: dollarMatch ? dollarMatch[1] : '',
                    isDollar: !!dollarMatch,
                    name: stockName,
                    change: changeMatch ? changeMatch[0] : '0%',
                    volume: vol,
                    isEtf: !hasKorean
                });
            }
            return { rows: result, fxRate: fxRate };
        }""")

        # JS 결과에서 환율과 종목 데이터 분리
        fx_rate = rows.get("fxRate", 0) if isinstance(rows, dict) else 0
        stock_rows = rows.get("rows", []) if isinstance(rows, dict) else rows
        if fx_rate > 0:
            self._fx_rate = fx_rate
            logger.info("실시간 환율: %.2f원/$", fx_rate)
        else:
            # 환율 추출 실패 시 기존 값 유지 (최초는 1500 기본값)
            if self._fx_rate == 0:
                self._fx_rate = 1500.0
            logger.warning("환율 추출 실패 — 기존 환율 사용: %.2f", self._fx_rate)

        cache: dict[str, StockPrice] = {}
        rank_cache: dict[str, int] = {}
        rank = 0
        dollar_count = 0
        for row in stock_rows:
            raw = row.get("priceRaw", "").replace(",", "")
            if not raw:
                continue
            try:
                if row["isDollar"]:
                    price = int(float(raw) * self._fx_rate)
                    dollar_count += 1
                else:
                    price = int(raw)
            except ValueError:
                continue
            cache[row["code"]] = StockPrice(
                code=row["code"],
                name=row.get("name", row["code"]),
                current_price=price,
                change_rate=_parse_rate(row.get("change", "0%")),
                volume=row.get("volume", 0),
                is_etf=row.get("isEtf", False),
            )
            rank_cache[row["code"]] = rank
            rank += 1
        if dollar_count > 0:
            logger.info("달러→원 환산: %d개 종목 (환율 %.2f 적용)", dollar_count, self._fx_rate)

        self._us_price_cache = cache
        self._us_rank_cache = rank_cache
        self._us_price_cache_time = time.time()
        logger.info("US 시세 캐시 갱신: %d개 종목", len(cache))

    async def _recover_page(self) -> Page:
        """메인 페이지가 닫힌 경우 같은 컨텍스트에서 새 탭을 생성."""
        if self._page and not self._page.is_closed():
            return self._page
        if not self._browser or not self._browser.is_connected():
            raise RuntimeError("브라우저가 종료되었습니다. 프로그램을 재시작해주세요.")
        contexts = self._browser.contexts
        if not contexts:
            raise RuntimeError("브라우저 컨텍스트가 없습니다. 프로그램을 재시작해주세요.")
        logger.warning("메인 페이지가 닫힘 — 새 탭을 생성합니다.")
        self._page = await contexts[0].new_page()
        self._page.set_default_timeout(self._config.timeout)
        return self._page

    async def _place_order(
        self, code: str, quantity: int, price: int, is_buy: bool
    ) -> OrderResult:
        """매수/매도 공통 주문 로직."""
        page = await self._recover_page()
        order_type = "매수" if is_buy else "매도"
        logger.info("%s 주문: %s %s주 (가격: %s)", order_type, code, str(quantity), price or "시장가")

        try:
            await page.bring_to_front()  # 백그라운드 탭 렌더링 제한 방지
            await page.goto(f"{self._config.login_url}/stocks/{code}", wait_until="domcontentloaded")
            await asyncio.sleep(2)  # React 하이드레이션 대기
            await page.wait_for_selector(SEL_STOCK_PRICE, timeout=20000)

            # 매수/매도 탭 클릭 (텍스트 기반 — 인덱스 의존 제거)
            tab_keyword = "구매" if is_buy else "판매"
            clicked = await page.evaluate(
                """(keyword) => {
                    const btns = document.querySelectorAll('#trade-order-section button');
                    for (const btn of btns) {
                        const t = btn.innerText.trim();
                        // 탭 버튼: 정확히 "구매" 또는 "판매" (제출 버튼 "구매하기" 등 제외)
                        if (t === keyword) { btn.click(); return t; }
                    }
                    return null;
                }""", tab_keyword,
            )
            if not clicked:
                raise RuntimeError(f"'{tab_keyword}' 탭 버튼을 찾을 수 없습니다")
            logger.info("%s 탭 선택 완료: '%s'", order_type, clicked)
            await asyncio.sleep(2)  # 주문 UI 렌더링 대기

            # 탭 선택 검증: 제출 버튼 텍스트가 의도한 방향과 일치하는지 확인
            submit_check = await page.evaluate(
                """(keyword) => {
                    const btns = document.querySelectorAll('#trade-order-section button');
                    for (const btn of btns) {
                        const t = btn.innerText.trim();
                        if (t.includes(keyword) && t.includes('하기')) return t;
                    }
                    return null;
                }""", tab_keyword,
            )
            if not submit_check:
                raise RuntimeError(f"탭 선택 검증 실패: '{tab_keyword}' 제출 버튼이 보이지 않습니다")

            # 시장가 버튼 클릭 (JS — 텍스트 "시장가"로 탐색)
            if price == 0:
                found = await page.evaluate("""() => {
                    const btns = document.querySelectorAll('#trade-order-section button');
                    for (const btn of btns) {
                        if (btn.innerText.trim() === '시장가') { btn.click(); return true; }
                    }
                    return false;
                }""")
                if not found:
                    raise RuntimeError("시장가 버튼을 찾을 수 없습니다")
                await asyncio.sleep(0.3)
            else:
                price_input = await page.query_selector(SEL_ORDER_PRICE_INPUT)
                if price_input:
                    await price_input.fill("")
                    await price_input.type(str(price))

            # 수량 입력
            qty_el = await page.wait_for_selector(SEL_ORDER_QTY_INPUT, timeout=5000)
            await qty_el.click()
            await asyncio.sleep(0.3)
            select_all = "Meta+a" if sys.platform == "darwin" else "Control+a"
            await page.keyboard.press(select_all)
            await page.keyboard.type(str(quantity))
            await asyncio.sleep(0.5)

            # 제출 버튼 클릭
            submit_keyword = tab_keyword  # 탭과 동일한 키워드
            logger.info("%s 제출 버튼 클릭 시도: %s %s주", order_type, code, str(quantity))
            submitted = await page.evaluate(
                """(keyword) => {
                    const btns = document.querySelectorAll('#trade-order-section button');
                    for (const btn of btns) {
                        const t = btn.innerText.trim();
                        if (t.includes(keyword) && t.includes('하기')) { btn.click(); return t; }
                    }
                    return null;
                }""", submit_keyword,
            )
            if not submitted:
                raise RuntimeError(f"{order_type} 제출 버튼을 찾을 수 없습니다")
            logger.info("%s 제출 버튼 클릭됨: '%s'", order_type, submitted)

            # 체결 확인: 주문 후 페이지 변화 감지
            await asyncio.sleep(3)
            # 주문 성공 시 토스트/알림 또는 페이지 상태 변화 확인
            verify = await page.evaluate("""() => {
                // 주문 영역 + 모달/토스트에서만 실패 키워드 확인 (페이지 전체 오탐 방지)
                const section = document.querySelector('#trade-order-section');
                const modal = document.querySelector('[role="dialog"], [class*="modal"], [class*="toast"]');
                const areas = [section, modal].filter(Boolean);
                const text = areas.map(a => a.innerText).join(' ');
                const failKeywords = ['주문 실패', '잔고 부족', '주문이 불가', '거래 불가',
                                       '거래할 수 없', '주문할 수 없'];
                for (const kw of failKeywords) {
                    if (text.includes(kw)) return { ok: false, reason: kw };
                }
                return { ok: true, reason: '주문 전송 완료' };
            }""")

            if not verify.get("ok", False):
                fail_reason = verify.get("reason", "알 수 없는 오류")
                logger.error("%s 체결 실패 감지: %s — %s", order_type, code, fail_reason)
                return OrderResult(success=False, message=f"체결 실패: {fail_reason}")

            logger.info("%s 주문 완료: %s %d주", order_type, code, quantity)
            return OrderResult(success=True, message=f"{order_type} 주문 전송 완료")

        except Exception as e:
            logger.error("%s 주문 실패: %s", order_type, e)
            return OrderResult(success=False, message=str(e))

    async def buy(self, code: str, quantity: int, price: int = 0) -> OrderResult:
        """매수 주문."""
        return await self._place_order(code, quantity, price, is_buy=True)

    async def sell(self, code: str, quantity: int, price: int = 0) -> OrderResult:
        """매도 주문."""
        return await self._place_order(code, quantity, price, is_buy=False)

    async def refresh_us_cache_if_needed(self) -> bool:
        """필요 시 US 캐시 갱신. 실제로 갱신된 경우에만 True 반환."""
        if time.time() - self._us_price_cache_time > 60 or not self._us_price_cache:
            prev_time = self._us_price_cache_time
            await self._refresh_us_price_cache()
            # 실제로 캐시가 갱신된 경우에만 True (페이지 로드 실패 시 False)
            return self._us_price_cache_time > prev_time
        return False

    def get_us_ranks(self) -> dict[str, int]:
        """현재 캐시의 US 종목 순위 반환 (0-indexed)."""
        return dict(self._us_rank_cache)

    async def keep_alive(self) -> None:
        """메인 페이지 세션 유지. 장시간 방치 시 세션 만료 방지."""
        try:
            page = await self._recover_page()
            await page.goto(
                f"{self._config.login_url}/",
                wait_until="domcontentloaded",
                timeout=15000,
            )
            logger.debug("메인 페이지 keep-alive 완료")
        except Exception as e:
            logger.warning("메인 페이지 keep-alive 실패: %s", e)

    async def close(self) -> None:
        """브라우저 종료."""
        if self._chart_page:
            await self._chart_page.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        logger.info("브라우저 종료 완료")
