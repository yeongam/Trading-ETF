"""종목 필터링 로직 - 가격·거래량·모멘텀 기반."""

from __future__ import annotations

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
