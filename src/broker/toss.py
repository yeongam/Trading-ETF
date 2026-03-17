"""토스증권 Playwright 기반 브로커 구현.

토스증권은 공식 API가 없으므로 웹 브라우저 자동화로 거래를 수행합니다.
로그인은 토스 앱 인증(QR 또는 알림)을 사용하므로 최초 1회 수동 인증이 필요합니다.
"""

import asyncio
import logging
from playwright.async_api import async_playwright, Browser, Page, Playwright

from .base import BaseBroker, StockPrice, OrderResult, Position
from ..config import TossConfig

logger = logging.getLogger(__name__)


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

        # 사용자가 토스 앱으로 인증 완료할 때까지 대기
        # 로그인 성공 시 URL이 변경되거나 특정 요소가 나타남
        logger.info("토스 앱에서 로그인을 승인해주세요...")
        try:
            await page.wait_for_url(
                f"{self._config.login_url}/**",
                timeout=120000,  # 2분 대기
            )
            # 로그인 후 메인 페이지 요소 확인
            await page.wait_for_selector('[class*="main"], [class*="home"]', timeout=30000)
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
            balance_el = await page.wait_for_selector(
                '[class*="balance"], [class*="cash"]', timeout=10000
            )
            if balance_el:
                text = await balance_el.inner_text()
                return int(text.replace(",", "").replace("원", "").strip())
        except Exception as e:
            logger.error("예수금 조회 실패: %s", e)
        return 0

    async def get_positions(self) -> list[Position]:
        """보유 종목 조회."""
        page = self.page
        await page.goto(f"{self._config.login_url}/my/stocks")
        positions: list[Position] = []
        try:
            await page.wait_for_selector('[class*="stock-item"], [class*="holding"]', timeout=10000)
            items = await page.query_selector_all('[class*="stock-item"], [class*="holding"]')
            for item in items:
                text = await item.inner_text()
                # 토스증권 UI 구조에 맞게 파싱 (실제 DOM 구조에 따라 조정 필요)
                logger.debug("보유종목 항목: %s", text)
                # TODO: 실제 토스증권 DOM 구조에 맞춰 파싱 로직 구현
        except Exception as e:
            logger.warning("보유종목 조회 실패: %s", e)
        return positions

    async def get_price(self, code: str) -> StockPrice | None:
        """종목 현재가 조회."""
        page = self.page
        await page.goto(f"{self._config.login_url}/stocks/{code}")
        try:
            await page.wait_for_selector('[class*="price"], [class*="current"]', timeout=10000)
            price_el = await page.query_selector('[class*="current-price"], [class*="price"]')
            name_el = await page.query_selector('[class*="stock-name"], h1, h2')

            price_text = await price_el.inner_text() if price_el else "0"
            name_text = await name_el.inner_text() if name_el else code

            return StockPrice(
                code=code,
                name=name_text.strip(),
                current_price=int(price_text.replace(",", "").replace("원", "").strip()),
                change_rate=0.0,  # TODO: 파싱 추가
                volume=0,  # TODO: 파싱 추가
            )
        except Exception as e:
            logger.error("시세 조회 실패 [%s]: %s", code, e)
            return None

    async def buy(self, code: str, quantity: int, price: int = 0) -> OrderResult:
        """매수 주문."""
        page = self.page
        logger.info("매수 주문: %s %d주 (가격: %s)", code, quantity, price or "시장가")
        try:
            await page.goto(f"{self._config.login_url}/stocks/{code}")
            await page.wait_for_selector('[class*="buy"], button:has-text("매수")', timeout=10000)

            # 매수 버튼 클릭
            buy_btn = await page.query_selector('button:has-text("매수")')
            if buy_btn:
                await buy_btn.click()

            # 수량 입력
            qty_input = await page.wait_for_selector('input[type="number"], input[class*="quantity"]')
            if qty_input:
                await qty_input.fill(str(quantity))

            # 가격 입력 (지정가인 경우)
            if price > 0:
                price_input = await page.query_selector('input[class*="price"]')
                if price_input:
                    await price_input.fill(str(price))

            # 주문 확인
            confirm_btn = await page.query_selector('button:has-text("확인"), button:has-text("주문")')
            if confirm_btn:
                await confirm_btn.click()

            await asyncio.sleep(2)
            logger.info("매수 주문 완료")
            return OrderResult(success=True, message="매수 주문 전송 완료")
        except Exception as e:
            logger.error("매수 주문 실패: %s", e)
            return OrderResult(success=False, message=str(e))

    async def sell(self, code: str, quantity: int, price: int = 0) -> OrderResult:
        """매도 주문."""
        page = self.page
        logger.info("매도 주문: %s %d주 (가격: %s)", code, quantity, price or "시장가")
        try:
            await page.goto(f"{self._config.login_url}/stocks/{code}")
            await page.wait_for_selector('[class*="sell"], button:has-text("매도")', timeout=10000)

            sell_btn = await page.query_selector('button:has-text("매도")')
            if sell_btn:
                await sell_btn.click()

            qty_input = await page.wait_for_selector('input[type="number"], input[class*="quantity"]')
            if qty_input:
                await qty_input.fill(str(quantity))

            if price > 0:
                price_input = await page.query_selector('input[class*="price"]')
                if price_input:
                    await price_input.fill(str(price))

            confirm_btn = await page.query_selector('button:has-text("확인"), button:has-text("주문")')
            if confirm_btn:
                await confirm_btn.click()

            await asyncio.sleep(2)
            logger.info("매도 주문 완료")
            return OrderResult(success=True, message="매도 주문 전송 완료")
        except Exception as e:
            logger.error("매도 주문 실패: %s", e)
            return OrderResult(success=False, message=str(e))

    async def close(self) -> None:
        """브라우저 종료."""
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        logger.info("브라우저 종료 완료")
