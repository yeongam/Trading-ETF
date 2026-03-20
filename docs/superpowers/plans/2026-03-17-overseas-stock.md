# 해외주식 자동매매 구현 계획

> **For agentic workers:** Use superpowers:subagent-driven-development to implement this plan.

**Goal:** 토스증권 해외주식 거래대금 차트에서 자동으로 종목을 발굴해 미국 장 시간에 매매한다.

**Architecture:**
- `TossBroker`에 `scrape_overseas_candidates()` 추가 — 로그인된 Playwright 페이지로 토스 차트를 스크래핑해 US 종목 코드 수집
- `TradingConfig.market = "us"` 필드로 한국장/해외장 전환
- `_is_market_open()`을 미국 동부시간 기준으로 교체 (zoneinfo 사용)
- `_get_watchlist()`를 async로 변경, market에 따라 적절한 스크리너 호출

**Tech Stack:** Python 3.11, Playwright (기존), zoneinfo (내장), 기존 전략/리스크 그대로 재사용

---

## 파일 구조

| 상태 | 경로 | 변경 내용 |
|------|------|-----------|
| 수정 | `src/broker/toss.py` | `scrape_overseas_candidates()` 메서드 추가 |
| 수정 | `src/config.py` | `TradingConfig`에 `market: str = "us"` 추가 |
| 수정 | `src/scheduler.py` | 미국장 시간 + async `_get_watchlist()` + 해외 스크리너 연동 |
| 수정 | `config.json` | `"market": "us"` 반영 |

---

## Chunk 1: 브로커에 해외 종목 스크래핑 추가

### Task 1: toss.py — scrape_overseas_candidates() 추가

**Files:**
- Modify: `src/broker/toss.py`

현재 `toss.py`의 `close()` 메서드 **위에** 다음 메서드를 추가한다.

- [ ] **Step 1: scrape_overseas_candidates() 추가**

```python
async def scrape_overseas_candidates(self, max_stocks: int = 30) -> list[tuple[str, str]]:
    """해외주식 거래대금 상위 종목 목록을 스크래핑한다.

    토스증권 실시간 거래대금 차트 페이지에서 US 종목 코드와 이름을 수집합니다.
    로그인이 완료된 상태에서 호출해야 합니다.

    Returns:
        list of (code, name) — 예: [("US20201215010", "메이우 테크놀로지"), ...]
    """
    page = self.page
    chart_url = (
        f"{self._config.login_url}"
        "/?market=us&live-chart=biggest_total_amount"
    )
    logger.info("해외주식 목록 스크래핑: %s", chart_url)

    try:
        await page.goto(chart_url)
        # [TUNE] US 종목 링크가 로드될 때까지 대기
        await page.wait_for_selector('a[href*="/stocks/US"]', timeout=20000)

        links = await page.query_selector_all('a[href*="/stocks/US"]')
        candidates: list[tuple[str, str]] = []
        seen: set[str] = set()

        for link in links:
            href = await link.get_attribute("href") or ""
            code_match = re.search(r"/stocks/(US\w+)", href)
            if not code_match:
                continue
            code = code_match.group(1)
            if code in seen:
                continue
            seen.add(code)

            # [TUNE] 종목명 — 링크 내부의 텍스트 요소
            name_el = await link.query_selector(
                "span, [class*='name'], [class*='title'], p"
            )
            name = (await name_el.inner_text()).strip() if name_el else code

            candidates.append((code, name))
            if len(candidates) >= max_stocks:
                break

        logger.info("해외주식 %d개 종목 수집 완료", len(candidates))
        return candidates

    except Exception as e:
        logger.error("해외주식 스크래핑 실패: %s", e)
        return []
```

- [ ] **Step 2: import 확인**

```bash
python3.11 -c "
import sys; sys.path.insert(0, '.')
from src.broker.toss import TossBroker
print(hasattr(TossBroker, 'scrape_overseas_candidates'))
print('OK')
"
```

Expected: `True` + `OK`

---

## Chunk 2: config + 스케줄러 미국장 전환

### Task 2: config.py — market 필드 추가

**Files:**
- Modify: `src/config.py`

- [ ] **Step 1: TradingConfig에 market 추가**

```python
@dataclass
class TradingConfig:
    """매매 관련 설정."""

    watchlist: list[str] = field(default_factory=list)  # 비어있으면 스크리너 사용
    check_interval: int = 60          # 초
    max_buy_amount: int = 0           # 1회 최대 매수 금액 (0=무제한)
    total_budget: int = 100_000       # 총 투자 예산 (원)
    market: str = "us"                # "ko" = 한국장, "us" = 미국장
    dry_run: bool = True
```

- [ ] **Step 2: 검증**

```bash
python3.11 -c "
import sys; sys.path.insert(0, '.')
from src.config import TradingConfig
cfg = TradingConfig()
assert cfg.market == 'us'
print('OK')
"
```

---

### Task 3: scheduler.py — 미국장 시간 + 해외 스크리너 연동

**Files:**
- Modify: `src/scheduler.py`

#### 3-1. import 추가

파일 상단 import 블록에 추가:
```python
from zoneinfo import ZoneInfo
from .broker.toss import TossBroker
```

#### 3-2. `_is_market_open()` 교체

기존 메서드 전체를 다음으로 교체:

```python
def _is_market_open(self) -> bool:
    """장 운영 시간 확인.

    market=ko: 평일 09:00~15:30 KST
    market=us: 평일 09:30~16:00 ET (EDT/EST 자동 반영)
    """
    if self._config.market == "us":
        eastern = ZoneInfo("America/New_York")
        now = datetime.now(eastern)
        if now.weekday() >= 5:
            return False
        open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
        close_time = now.replace(hour=16, minute=0, second=0, microsecond=0)
        return open_time <= now <= close_time
    else:
        now = datetime.now()
        if now.weekday() >= 5:
            return False
        open_time = now.replace(hour=9, minute=0, second=0, microsecond=0)
        close_time = now.replace(hour=15, minute=30, second=0, microsecond=0)
        return open_time <= now <= close_time
```

#### 3-3. `_get_watchlist()` → `async` + 해외 스크리너 분기

기존 `_get_watchlist()` 전체를 다음으로 교체:

```python
async def _get_watchlist(self) -> list[str]:
    """감시 종목 반환.

    watchlist 설정 시 그대로 사용.
    비어있으면 market에 따라 자동 스크리닝:
    - us: 토스 해외주식 거래대금 상위 스크래핑
    - ko: pykrx KOSPI/KOSDAQ 스크리닝
    """
    if self._config.watchlist:
        return self._config.watchlist

    now = time.time()
    if now - self._last_screen_time < _SCREEN_INTERVAL and self._cached_watchlist:
        return self._cached_watchlist

    if self._config.market == "us":
        logger.info("해외주식 종목 스크리닝 시작...")
        if isinstance(self._broker, TossBroker):
            pairs = await self._broker.scrape_overseas_candidates()
            active_codes = set(self._risk.positions.keys()) if self._risk else set()
            self._cached_watchlist = [
                code for code, _ in pairs if code not in active_codes
            ]
        else:
            logger.warning("TossBroker가 아니므로 해외 스크리닝 불가")
            self._cached_watchlist = []
    else:
        logger.info("국내주식 종목 스크리닝 시작...")
        active_codes = set(self._risk.positions.keys()) if self._risk else set()
        candidates = fetch_candidates()
        self._cached_watchlist = screen(
            candidates, self._screener_config, exclude_codes=active_codes
        )

    self._last_screen_time = now
    logger.info("스크리닝 완료: %s", self._cached_watchlist[:5])
    return self._cached_watchlist
```

#### 3-4. `run_once()` — await 추가

`_get_watchlist()` 호출 부분 수정 (`async` 변경에 따른 await):

```python
async def run_once(self) -> list[dict]:
    results = []
    watchlist = await self._get_watchlist()   # ← await 추가
    if not watchlist:
        logger.warning("감시 종목 없음 (스크리닝 결과 0개)")
        return results
    ...  # 나머지 기존 코드 그대로
```

- [ ] **Step 1: 위 변경사항 모두 적용**

- [ ] **Step 2: 검증**

```bash
python3.11 -c "
import sys; sys.path.insert(0, '.')
from src.scheduler import TradingScheduler
from zoneinfo import ZoneInfo
from datetime import datetime
eastern = ZoneInfo('America/New_York')
now = datetime.now(eastern)
print('현재 미국 동부시간:', now.strftime('%Y-%m-%d %H:%M %Z'))
print('OK')
"
```

Expected: 현재 ET 시각 출력 + OK

---

### Task 4: config.json — market 반영

**Files:**
- Modify: `config.json`

```json
{
  "toss": {
    "login_url": "https://www.tossinvest.com",
    "headless": false,
    "slow_mo": 100,
    "timeout": 30000
  },
  "trading": {
    "watchlist": [],
    "check_interval": 60,
    "max_buy_amount": 0,
    "total_budget": 100000,
    "market": "us",
    "dry_run": true
  },
  "dashboard": {
    "host": "127.0.0.1",
    "port": 8080
  }
}
```

- [ ] **검증**

```bash
python3.11 -c "
import sys; sys.path.insert(0, '.')
from src.config import AppConfig
cfg = AppConfig.load()
assert cfg.trading.market == 'us'
print('market:', cfg.trading.market)
print('OK')
"
```

---

## 최종 통합 확인

- [ ] **전체 모듈 로딩**

```bash
python3.11 -c "
import sys; sys.path.insert(0, '.')
from src.config import AppConfig
from src.broker.toss import TossBroker
from src.scheduler import TradingScheduler
from src.risk import RiskManager, RiskConfig
from src.strategy.multi_confirm import MultiConfirmStrategy
print('모든 모듈 로딩 OK')
"
```

Expected: `모든 모듈 로딩 OK`

---

## 참고: 셀렉터 튜닝 안내

`scrape_overseas_candidates()` 안의 `[TUNE]` 셀렉터는 실제 토스 DOM에 따라 조정 필요.
첫 실행 후 종목이 0개로 나오면:
1. 브라우저에서 `https://www.tossinvest.com/?market=us&live-chart=biggest_total_amount` 열기
2. F12 → 종목명 요소 우클릭 → Copy selector
3. `toss.py`의 `[TUNE]` 주석 부분 수정

## 참고: 미국장 거래 가능 시간 (KST 기준)

| 기간 | 미국 EDT (3~11월) | 미국 EST (11~3월) |
|------|-------------------|-------------------|
| 개장 | 오후 10:30 KST | 오후 11:30 KST |
| 폐장 | 새벽 5:00 KST | 새벽 6:00 KST |
