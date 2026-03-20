# Auto Stock Screener + Budget Cap Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** watchlist 없이 자동으로 종목을 발굴하고, 10만원 예산 내에서만 매매한다.

**Architecture:** `pykrx` 라이브러리로 KOSPI/KOSDAQ 전체 종목을 가져와 가격·거래량·모멘텀으로 필터링하는 Screener 모듈을 추가한다. RiskManager에 예산 추적 기능을 붙여 총 투자금이 `total_budget`을 초과하면 신규 진입을 막는다. Scheduler는 매 사이클마다 Screener에서 종목 후보를 받아 기존 전략을 돌린다.

**Tech Stack:** Python 3.11, pykrx (KRX/Naver Finance 데이터), 기존 Playwright/FastAPI 스택

---

## 파일 구조

| 상태 | 경로 | 역할 |
|------|------|------|
| 신규 | `src/screener/__init__.py` | 패키지 |
| 신규 | `src/screener/krx.py` | pykrx로 종목 목록·OHLCV 조회 |
| 신규 | `src/screener/filter.py` | 가격·거래량·모멘텀 필터링 → 후보 리스트 |
| 수정 | `src/config.py` | `TradingConfig`에 `total_budget: int = 100000` 추가, `watchlist` 선택으로 변경 |
| 수정 | `src/risk.py` | `RiskManager`에 `_total_invested` 추적, `can_open_position()`·`calculate_position_size()`에 예산 체크 |
| 수정 | `src/scheduler.py` | `run_once()`에서 watchlist 없으면 Screener 호출 |
| 수정 | `requirements.txt` | `pykrx>=0.1.49` 추가 |

---

## Chunk 1: pykrx 설치 + KRX 데이터 조회 모듈

### Task 1: requirements.txt + pykrx 설치

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: pykrx 추가**

```
playwright>=1.40.0
fastapi>=0.104.0
uvicorn>=0.24.0
pykrx>=0.1.49
```

- [ ] **Step 2: 설치**

```bash
python3.11 -m pip install pykrx
```

Expected: `Successfully installed pykrx-...`

- [ ] **Step 3: 동작 확인**

```bash
python3.11 -c "from pykrx import stock; print(stock.get_market_ticker_list(market='KOSPI')[:3])"
```

Expected: `['095570', '006840', '027830']` 같은 종목 코드 배열

---

### Task 2: KRX 조회 모듈 (`src/screener/krx.py`)

**Files:**
- Create: `src/screener/__init__.py`
- Create: `src/screener/krx.py`

- [ ] **Step 1: 패키지 파일 생성**

`src/screener/__init__.py`:
```python
"""주식 스크리닝 모듈."""
```

- [ ] **Step 2: krx.py 구현**

`src/screener/krx.py`:
```python
"""pykrx 기반 KRX 종목 데이터 조회."""

import logging
from datetime import datetime, timedelta
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class StockCandidate:
    """스크리닝 후보 종목."""
    code: str
    name: str
    price: int
    volume: int
    change_rate: float  # 5일 수익률 (%)


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def _days_ago(n: int) -> str:
    return (datetime.now() - timedelta(days=n)).strftime("%Y%m%d")


def fetch_candidates(markets: list[str] | None = None) -> list[StockCandidate]:
    """KOSPI/KOSDAQ 종목 목록과 최근 시세를 조회한다."""
    try:
        from pykrx import stock as krx
    except ImportError:
        logger.error("pykrx 미설치. `pip install pykrx` 실행 필요")
        return []

    if markets is None:
        markets = ["KOSPI", "KOSDAQ"]

    today = _today()
    five_days_ago = _days_ago(7)  # 주말 포함해 7일치 요청
    candidates: list[StockCandidate] = []

    for market in markets:
        try:
            tickers = krx.get_market_ticker_list(market=market)
            for code in tickers:
                try:
                    ohlcv = krx.get_market_ohlcv_by_date(five_days_ago, today, code)
                    if ohlcv.empty or len(ohlcv) < 2:
                        continue

                    latest = ohlcv.iloc[-1]
                    oldest = ohlcv.iloc[0]

                    price = int(latest["종가"])
                    volume = int(latest["거래량"])
                    if oldest["종가"] > 0:
                        change_rate = (latest["종가"] - oldest["종가"]) / oldest["종가"] * 100
                    else:
                        change_rate = 0.0

                    name = krx.get_market_ticker_name(code)
                    candidates.append(StockCandidate(
                        code=code,
                        name=name,
                        price=price,
                        volume=volume,
                        change_rate=round(change_rate, 2),
                    ))
                except Exception as e:
                    logger.debug("종목 조회 실패 [%s]: %s", code, e)
                    continue
        except Exception as e:
            logger.error("시장 조회 실패 [%s]: %s", market, e)

    return candidates
```

- [ ] **Step 3: 동작 확인 (소수 종목만 빠르게)**

```bash
python3.11 -c "
import sys; sys.path.insert(0, '.')
from src.screener.krx import fetch_candidates
result = fetch_candidates(['KOSPI'])
print(f'조회된 종목 수: {len(result)}')
if result:
    print(result[0])
"
```

Expected: `조회된 종목 수: 800+` 및 StockCandidate 출력

---

### Task 3: 필터링 모듈 (`src/screener/filter.py`)

**Files:**
- Create: `src/screener/filter.py`

- [ ] **Step 1: filter.py 구현**

`src/screener/filter.py`:
```python
"""종목 필터링 로직 - 가격·거래량·모멘텀 기반."""

import logging
from dataclasses import dataclass

from .krx import StockCandidate

logger = logging.getLogger(__name__)


@dataclass
class ScreenerConfig:
    """스크리너 설정."""
    min_price: int = 1000          # 최소 주가 (원)
    max_price: int = 50000         # 최대 주가 (10만원으로 2주 이상 살 수 있는 가격)
    min_volume: int = 500_000      # 최소 거래량 (유동성)
    min_momentum: float = 0.0      # 5일 최소 수익률 (%)
    max_candidates: int = 20       # 최대 후보 수


def screen(
    candidates: list[StockCandidate],
    config: ScreenerConfig,
    exclude_codes: set[str] | None = None,
) -> list[str]:
    """후보 목록을 필터링해 종목 코드 리스트를 반환한다.

    필터 조건:
    1. 가격: min_price ~ max_price
    2. 거래량: >= min_volume
    3. 5일 모멘텀: >= min_momentum
    4. 이미 보유 중인 종목 제외
    정렬: 거래량 내림차순 (유동성 우선)
    """
    exclude = exclude_codes or set()
    filtered = [
        c for c in candidates
        if (
            config.min_price <= c.price <= config.max_price
            and c.volume >= config.min_volume
            and c.change_rate >= config.min_momentum
            and c.code not in exclude
        )
    ]

    # 거래량 내림차순 정렬
    filtered.sort(key=lambda c: c.volume, reverse=True)

    result = [c.code for c in filtered[: config.max_candidates]]
    logger.info("스크리닝 결과: %d개 후보 (전체 %d개 중)", len(result), len(candidates))
    return result
```

- [ ] **Step 2: 테스트**

```bash
python3.11 -c "
import sys; sys.path.insert(0, '.')
from src.screener.krx import StockCandidate
from src.screener.filter import screen, ScreenerConfig

dummy = [
    StockCandidate('000001', '테스트A', 5000, 1_000_000, 1.5),
    StockCandidate('000002', '테스트B', 200000, 2_000_000, 2.0),   # 가격 초과 → 제외
    StockCandidate('000003', '테스트C', 3000, 100_000, 0.5),       # 거래량 부족 → 제외
]
result = screen(dummy, ScreenerConfig())
print('통과:', result)
assert result == ['000001'], f'예상: [000001], 실제: {result}'
print('OK')
"
```

Expected: `통과: ['000001']` + `OK`

---

## Chunk 2: 예산 캡 (RiskManager 수정)

### Task 4: config.py — total_budget 추가

**Files:**
- Modify: `src/config.py`

- [ ] **Step 1: TradingConfig에 total_budget 추가**

`watchlist` 의 기본값을 빈 리스트로 유지하고 `total_budget` 필드를 추가:

```python
@dataclass
class TradingConfig:
    """매매 관련 설정."""

    watchlist: list[str] = field(default_factory=list)  # 비어있으면 스크리너 사용
    check_interval: int = 10          # 초
    max_buy_amount: int = 0           # 1회 최대 매수 금액 (0=무제한)
    total_budget: int = 100_000       # 총 투자 예산 (원). 잔고 전체 사용 방지
    dry_run: bool = True
```

- [ ] **Step 2: 동작 확인**

```bash
python3.11 -c "
import sys; sys.path.insert(0, '.')
from src.config import TradingConfig
cfg = TradingConfig()
print('total_budget:', cfg.total_budget)
assert cfg.total_budget == 100_000
print('OK')
"
```

Expected: `total_budget: 100000` + `OK`

---

### Task 5: risk.py — 예산 추적

**Files:**
- Modify: `src/risk.py`

- [ ] **Step 1: RiskManager에 budget 파라미터·추적 추가**

`__init__` 시그니처에 `total_budget: int = 100_000` 추가:

```python
class RiskManager:
    def __init__(self, config: RiskConfig, total_budget: int = 100_000) -> None:
        self._config = config
        self._total_budget = total_budget
        self._total_invested: int = 0          # 현재 투자 중인 금액 합계
        self._positions: dict[str, ActivePosition] = {}
        self._daily_pnl: float = 0.0
        self._consecutive_losses: int = 0
        self._cooldown_remaining: int = 0
        self._trade_count: int = 0
        self._win_count: int = 0
```

- [ ] **Step 2: stats에 예산 정보 추가**

```python
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
        "total_budget": self._total_budget,
        "total_invested": self._total_invested,
        "remaining_budget": self._total_budget - self._total_invested,
    }
```

- [ ] **Step 3: can_open_position()에 예산 체크 추가**

기존 코드 끝에 예산 체크 추가:

```python
def can_open_position(self, code: str) -> tuple[bool, str]:
    if code in self._positions:
        return False, f"이미 보유 중: {code}"
    if len(self._positions) >= self._config.max_positions:
        return False, f"최대 보유 종목 수 초과 ({self._config.max_positions})"
    if self._cooldown_remaining > 0:
        return False, f"쿨다운 중 (남은 횟수: {self._cooldown_remaining})"
    if self._daily_pnl <= -self._config.max_daily_loss_pct:
        return False, f"일일 최대 손실 도달 ({self._daily_pnl:.1f}%)"
    if self._total_invested >= self._total_budget:
        return False, f"예산 소진 (투자: {self._total_invested:,}원 / 예산: {self._total_budget:,}원)"
    return True, "OK"
```

- [ ] **Step 4: calculate_position_size()에 잔여 예산 캡 적용**

```python
def calculate_position_size(self, balance: int, entry_price: int) -> int:
    if entry_price <= 0:
        return 0
    risk_amount = balance * (self._config.max_risk_per_trade / 100)
    loss_per_share = entry_price * (self._config.stop_loss_pct / 100)
    if loss_per_share <= 0:
        return 0
    quantity = int(risk_amount / loss_per_share)

    # 잔여 예산 초과 방지
    remaining = self._total_budget - self._total_invested
    max_qty_by_budget = remaining // entry_price
    quantity = min(quantity, max_qty_by_budget)

    return max(0, quantity)
```

- [ ] **Step 5: open_position / close_position에서 투자금 갱신**

`open_position`:
```python
def open_position(self, code: str, entry_price: int, quantity: int) -> ActivePosition:
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
    self._total_invested += entry_price * quantity   # ← 추가
    logger.info(
        "포지션 진입: %s %d주 @ %d원 (손절: %.0f / 익절: %.0f) [투자: %d원 / 예산: %d원]",
        code, quantity, entry_price, stop_loss, take_profit,
        self._total_invested, self._total_budget,
    )
    return position
```

`close_position`의 `_positions.pop` 직후:
```python
self._total_invested = max(0, self._total_invested - position.entry_price * position.quantity)
```

- [ ] **Step 6: 예산 로직 검증**

```bash
python3.11 -c "
import sys; sys.path.insert(0, '.')
from src.risk import RiskManager, RiskConfig

rm = RiskManager(RiskConfig(), total_budget=100_000)

# 1) 60,000원짜리 포지션 진입
rm.open_position('A', 60_000, 1)
print('투자금:', rm.stats['total_invested'])  # 60000

# 2) 추가 진입 가능한지 확인 (40,000 남음)
ok, msg = rm.can_open_position('B')
print('B 진입 가능?', ok, msg)  # True

# 3) 50,000원 포지션 → 예산 초과
size = rm.calculate_position_size(1_000_000, 50_000)
print('B 수량 (max 0이어야):', size)  # 0 (잔여 40,000으로 50,000짜리 못 삼)

# 4) A 청산
rm.close_position('A', 62_000)
print('청산 후 투자금:', rm.stats['total_invested'])  # 0
"
```

Expected: 각 assert 통과

---

## Chunk 3: Scheduler — 자동 종목 발굴 연동

### Task 6: scheduler.py 수정

**Files:**
- Modify: `src/scheduler.py`

Screener를 optional 의존성으로 주입. watchlist가 비어있으면 screener를 호출한다.

- [ ] **Step 1: Screener import 및 생성자 파라미터 추가**

```python
from .screener.krx import fetch_candidates
from .screener.filter import screen, ScreenerConfig
```

생성자:
```python
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
    self._cached_watchlist: list[str] = list(config.watchlist)
    self._last_screen_time: float = 0.0
```

- [ ] **Step 2: _get_watchlist() 헬퍼 추가**

스크리닝 결과를 30분마다 갱신(API 부하 방지):

```python
import time

_SCREEN_INTERVAL = 1800  # 30분

def _get_watchlist(self) -> list[str]:
    """감시 종목 반환. watchlist 설정 없으면 자동 스크리닝."""
    if self._config.watchlist:
        return self._config.watchlist

    now = time.time()
    if now - self._last_screen_time < _SCREEN_INTERVAL and self._cached_watchlist:
        return self._cached_watchlist

    logger.info("종목 스크리닝 시작...")
    active_codes = set(self._risk.positions.keys()) if self._risk else set()
    candidates = fetch_candidates()
    self._cached_watchlist = screen(candidates, self._screener_config, exclude_codes=active_codes)
    self._last_screen_time = now
    logger.info("스크리닝 완료: %s", self._cached_watchlist[:5])
    return self._cached_watchlist
```

- [ ] **Step 3: run_once() 수정**

```python
async def run_once(self) -> list[dict]:
    results = []
    watchlist = self._get_watchlist()
    if not watchlist:
        logger.warning("감시 종목 없음 (스크리닝 결과 0개)")
        return results

    for code in watchlist:
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
```

- [ ] **Step 4: import 확인**

```bash
python3.11 -c "
import sys; sys.path.insert(0, '.')
from src.scheduler import TradingScheduler
print('OK')
"
```

Expected: `OK`

---

### Task 7: main.py — total_budget을 RiskManager에 전달

**Files:**
- Modify: `main.py` (RiskManager 생성 부분)

- [ ] **Step 1: main.py 확인 후 수정**

```bash
grep -n "RiskManager" main.py
```

`RiskManager(risk_config)` → `RiskManager(risk_config, total_budget=cfg.trading.total_budget)` 로 변경

- [ ] **Step 2: 전체 임포트 확인**

```bash
python3.11 -c "
import sys; sys.path.insert(0, '.')
import main
print('OK')
"
```

Expected: 에러 없이 `OK`

---

## 최종 확인

- [ ] **전체 모듈 로딩 확인**

```bash
python3.11 -c "
import sys; sys.path.insert(0, '.')
from src.config import AppConfig
from src.risk import RiskManager, RiskConfig
from src.screener.krx import fetch_candidates
from src.screener.filter import screen, ScreenerConfig
from src.scheduler import TradingScheduler
print('모든 모듈 로딩 OK')
"
```

- [ ] **드라이런 테스트**

```bash
python3.11 main.py --dry-run --dashboard-only &
sleep 3 && curl -s http://127.0.0.1:8080/status | python3.11 -m json.tool
```

Expected: `"dry_run": true`, `"total_budget": 100000` 포함된 JSON

---

## config.json 최종 예시

```json
{
  "toss": {
    "login_url": "https://tossinvest.com",
    "headless": false,
    "slow_mo": 100,
    "timeout": 30000
  },
  "trading": {
    "watchlist": [],
    "check_interval": 60,
    "max_buy_amount": 0,
    "total_budget": 100000,
    "dry_run": true
  },
  "dashboard": {
    "host": "127.0.0.1",
    "port": 8080
  }
}
```

`watchlist: []` → 자동 스크리닝 활성화
`total_budget: 100000` → 10만원 이상 투자 불가
`dry_run: true` → 충분히 테스트 후 `false`로 전환
