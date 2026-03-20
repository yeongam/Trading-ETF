"""pykrx 기반 KRX 종목 데이터 조회."""

from __future__ import annotations

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
