"""토스증권 Playwright 기반 브로커 구현.

토스증권은 공식 API가 없으므로 웹 브라우저 자동화로 거래를 수행합니다.
로그인은 토스 앱 인증(QR 또는 알림)을 사용하므로 최초 1회 수동 인증이 필요합니다.

=== 셀렉터 튜닝 가이드 ===
실제 토스증권 DOM과 다를 수 있는 셀렉터에는 [TUNE] 주석이 붙어 있습니다.
브라우저 DevTools(F12)에서 해당 요소를 우클릭 > Copy > Copy selector로
실제 셀렉터를 확인한 뒤 교체하세요.
"""

import asyncio
import logging
import re
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

# --- MY 페이지 (잔고/보유종목) ---
SEL_MY_BALANCE = '[class*="available"] span, [class*="investable"] span'  # [TUNE] "주문가능금액" 옆 숫자
SEL_MY_TOTAL_ASSET = '[class*="total-asset"] span'  # [TUNE] 총 자산 금액

# 보유종목 리스트
SEL_HOLDING_LIST = 'ul[class*="stock-list"] > li, [class*="holding-list"] > div'  # [TUNE] 보유종목 목록 컨테이너
SEL_HOLDING_NAME = '[class*="name"], a > span:first-child'  # [TUNE] 종목명
SEL_HOLDING_QTY = '[class*="quantity"], [class*="count"]'  # [TUNE] 보유수량
SEL_HOLDING_AVG_PRICE = '[class*="avg"], [class*="average"]'  # [TUNE] 평균단가
SEL_HOLDING_CUR_PRICE = '[class*="current"], [class*="price"]:last-child'  # [TUNE] 현재가

# --- 종목 상세 페이지 ---
SEL_STOCK_PRICE = '[class*="current-price"], [class*="stock-price"] > span'  # [TUNE] 현재가 (큰 숫자)
SEL_STOCK_NAME = 'h1, [class*="stock-name"], [class*="title"] > span'  # [TUNE] 종목명
SEL_STOCK_CHANGE = '[class*="change-rate"], [class*="rate"]'  # [TUNE] 등락률 (+2.35%)
SEL_STOCK_VOLUME = '[class*="volume"], td:has(+ td:has-text("거래량"))'  # [TUNE] 거래량

# --- 주문 UI ---
SEL_BUY_TAB = 'button:has-text("매수"), [role="tab"]:has-text("매수")'  # [TUNE] 매수 탭 버튼
SEL_SELL_TAB = 'button:has-text("매도"), [role="tab"]:has-text("매도")'  # [TUNE] 매도 탭 버튼
SEL_ORDER_QTY_INPUT = 'input[placeholder*="수량"], input[class*="qty"]'  # [TUNE] 수량 입력란
SEL_ORDER_PRICE_INPUT = 'input[placeholder*="가격"], input[class*="price"]'  # [TUNE] 가격 입력란
SEL_ORDER_MARKET_PRICE = 'button:has-text("시장가"), label:has-text("시장가")'  # [TUNE] 시장가 선택
SEL_ORDER_SUBMIT = 'button:has-text("매수하기"), button:has-text("매도하기")'  # [TUNE] 주문 제출
SEL_ORDER_CONFIRM = 'button:has-text("확인"), [class*="confirm"] button'  # [TUNE] 최종 확인 팝업


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

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("브라우저가 시작되지 않았습니다. login()을 먼저 호출하세요.")
        return self._page

    async def _launch_browser(self) -> Page:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self._config.headless,
            slow_mo=self._config.slow_mo,
        )
        context = await self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="ko-KR",
        )
        self._page = await context.new_page()
        self._page.set_default_timeout(self._config.timeout)
        return self._page

    async def login(self) -> bool:
        """토스증권 로그인.

        토스 앱 인증이 필요하므로 headless=False 상태에서
        사용자가 직접 QR/알림 인증을 완료해야 합니다.
        """
        page = await self._launch_browser()
        logger.info("토스증권 로그인 페이지로 이동합니다...")
        await page.goto(f"{self._config.login_url}/login")

        logger.info("토스 앱에서 로그인을 승인해주세요...")
        try:
            # 로그인 성공 시 URL이 /login에서 벗어남
            await page.wait_for_url(
                lambda url: "/login" not in url,
                timeout=120000,
            )
            # [TUNE] 로그인 후 메인 페이지의 특정 요소가 나타날 때까지 대기
            await page.wait_for_selector(SEL_LOGIN_COMPLETE, timeout=30000)
            logger.info("로그인 성공!")
            return True
        except Exception as e:
            logger.error("로그인 실패: %s", e)
            return False

    async def get_balance(self) -> int:
        """예수금(주문가능금액) 조회."""
        page = self.page
        await page.goto(f"{self._config.login_url}/my")
        try:
            # [TUNE] 주문가능금액 요소
            balance_el = await page.wait_for_selector(SEL_MY_BALANCE, timeout=10000)
            if balance_el:
                text = await balance_el.inner_text()
                return _parse_number(text)
        except Exception as e:
            logger.error("예수금 조회 실패: %s", e)
        return 0

    async def get_positions(self) -> list[Position]:
        """보유 종목 조회."""
        page = self.page
        await page.goto(f"{self._config.login_url}/my")
        positions: list[Position] = []
        try:
            # [TUNE] 보유종목 리스트 컨테이너
            await page.wait_for_selector(SEL_HOLDING_LIST, timeout=10000)
            items = await page.query_selector_all(SEL_HOLDING_LIST)

            for item in items:
                try:
                    name_el = await item.query_selector(SEL_HOLDING_NAME)
                    qty_el = await item.query_selector(SEL_HOLDING_QTY)
                    avg_el = await item.query_selector(SEL_HOLDING_AVG_PRICE)
                    cur_el = await item.query_selector(SEL_HOLDING_CUR_PRICE)

                    name = await name_el.inner_text() if name_el else "알수없음"
                    quantity = _parse_number(await qty_el.inner_text()) if qty_el else 0
                    avg_price = _parse_number(await avg_el.inner_text()) if avg_el else 0
                    cur_price = _parse_number(await cur_el.inner_text()) if cur_el else 0

                    if quantity > 0:
                        # 종목 링크에서 코드 추출 시도
                        link = await item.query_selector("a[href*='/stocks/']")
                        code = ""
                        if link:
                            href = await link.get_attribute("href") or ""
                            code_match = re.search(r"/stocks/(\w+)", href)
                            code = code_match.group(1) if code_match else ""

                        positions.append(Position(
                            code=code,
                            name=name.strip(),
                            quantity=quantity,
                            avg_price=avg_price,
                            current_price=cur_price,
                        ))
                except Exception as e:
                    logger.debug("보유종목 항목 파싱 실패: %s", e)
                    continue

        except Exception as e:
            logger.warning("보유종목 조회 실패: %s", e)
        return positions

    async def get_price(self, code: str) -> StockPrice | None:
        """종목 현재가 조회."""
        page = self.page
        await page.goto(f"{self._config.login_url}/stocks/{code}")
        try:
            # [TUNE] 현재가 요소
            await page.wait_for_selector(SEL_STOCK_PRICE, timeout=10000)

            price_el = await page.query_selector(SEL_STOCK_PRICE)
            name_el = await page.query_selector(SEL_STOCK_NAME)
            change_el = await page.query_selector(SEL_STOCK_CHANGE)
            volume_el = await page.query_selector(SEL_STOCK_VOLUME)

            price_text = await price_el.inner_text() if price_el else "0"
            name_text = await name_el.inner_text() if name_el else code
            change_text = await change_el.inner_text() if change_el else "0"
            volume_text = await volume_el.inner_text() if volume_el else "0"

            return StockPrice(
                code=code,
                name=name_text.strip(),
                current_price=_parse_number(price_text),
                change_rate=_parse_rate(change_text),
                volume=_parse_number(volume_text),
            )
        except Exception as e:
            logger.error("시세 조회 실패 [%s]: %s", code, e)
            return None

    async def _place_order(
        self, code: str, quantity: int, price: int, is_buy: bool
    ) -> OrderResult:
        """매수/매도 공통 주문 로직."""
        page = self.page
        order_type = "매수" if is_buy else "매도"
        logger.info("%s 주문: %s %d주 (가격: %s)", order_type, code, quantity, price or "시장가")

        try:
            await page.goto(f"{self._config.login_url}/stocks/{code}")
            await page.wait_for_selector(SEL_STOCK_PRICE, timeout=10000)

            # [TUNE] 매수/매도 탭 클릭
            tab_sel = SEL_BUY_TAB if is_buy else SEL_SELL_TAB
            tab_btn = await page.wait_for_selector(tab_sel, timeout=5000)
            if tab_btn:
                await tab_btn.click()
            await asyncio.sleep(0.5)

            # [TUNE] 시장가 주문이면 시장가 버튼 클릭
            if price == 0:
                market_btn = await page.query_selector(SEL_ORDER_MARKET_PRICE)
                if market_btn:
                    await market_btn.click()
                    await asyncio.sleep(0.3)
            else:
                # 지정가 입력
                price_input = await page.query_selector(SEL_ORDER_PRICE_INPUT)
                if price_input:
                    await price_input.fill("")
                    await price_input.type(str(price))

            # [TUNE] 수량 입력
            qty_input = await page.wait_for_selector(SEL_ORDER_QTY_INPUT, timeout=5000)
            if qty_input:
                await qty_input.fill("")
                await qty_input.type(str(quantity))

            # [TUNE] 주문 제출 (매수하기/매도하기)
            submit_text = "매수하기" if is_buy else "매도하기"
            submit_btn = await page.query_selector(f'button:has-text("{submit_text}")')
            if not submit_btn:
                submit_btn = await page.query_selector(SEL_ORDER_SUBMIT)
            if submit_btn:
                await submit_btn.click()

            # [TUNE] 최종 확인 팝업 (있는 경우)
            await asyncio.sleep(0.5)
            confirm_btn = await page.query_selector(SEL_ORDER_CONFIRM)
            if confirm_btn:
                await confirm_btn.click()

            await asyncio.sleep(1.5)
            logger.info("%s 주문 완료", order_type)
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

    async def close(self) -> None:
        """브라우저 종료."""
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        logger.info("브라우저 종료 완료")
