"""기술적 지표 계산 유틸리티.

순수 Python 구현으로 외부 의존성 없이 동작합니다.
모든 함수는 가격 리스트(최신이 마지막)를 입력받습니다.
"""

from __future__ import annotations


def sma(prices: list[float], period: int) -> float | None:
    """단순 이동평균."""
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period


def ema(prices: list[float], period: int) -> float | None:
    """지수 이동평균."""
    if len(prices) < period:
        return None
    multiplier = 2 / (period + 1)
    result = sum(prices[:period]) / period
    for price in prices[period:]:
        result = (price - result) * multiplier + result
    return result


def ema_series(prices: list[float], period: int) -> list[float]:
    """EMA 시리즈 반환 (최근 N개).

    Returns:
        EMA 값 리스트. 데이터 부족 시 빈 리스트.
    """
    if len(prices) < period:
        return []
    multiplier = 2 / (period + 1)
    result = sum(prices[:period]) / period
    series = [result]
    for price in prices[period:]:
        result = (price - result) * multiplier + result
        series.append(result)
    return series


def vwap(prices: list[float], volumes: list[int]) -> float | None:
    """VWAP (Volume Weighted Average Price).

    거래량 가중 평균단가. 주가가 VWAP 위에 있을 때만 매수 관점.

    Returns:
        거래량 가중 평균가. 데이터 부족 시 None.
    """
    if len(prices) < 2 or len(volumes) < 2:
        return None
    n = min(len(prices), len(volumes))
    total_pv = sum(prices[i] * volumes[i] for i in range(n))
    total_v = sum(volumes[:n])
    if total_v == 0:
        return None
    return total_pv / total_v


def rvol(volumes: list[int], period: int = 10) -> float | None:
    """상대 거래량 (RVOL).

    현재 거래량 / 과거 평균 거래량.

    Returns:
        2.0 이상이면 평소 대비 200% 이상 (거래량 폭발).
    """
    if len(volumes) < period + 1:
        return None
    avg = sum(volumes[-period - 1:-1]) / period
    if avg == 0:
        return None
    return volumes[-1] / avg


def rsi(prices: list[float], period: int = 14) -> float | None:
    """RSI (Relative Strength Index).

    Returns:
        0~100 사이 값. 30 이하=과매도, 70 이상=과매수.
    """
    if len(prices) < period + 1:
        return None

    gains = []
    losses = []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        gains.append(max(0, change))
        losses.append(max(0, -change))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def volume_ratio(volumes: list[int], period: int = 10) -> float | None:
    """거래량 비율 (현재 거래량 / 평균 거래량).

    Returns:
        1.0 이상이면 평균보다 거래량 많음.
    """
    if len(volumes) < period + 1:
        return None
    avg = sum(volumes[-period - 1 : -1]) / period
    if avg == 0:
        return None
    return volumes[-1] / avg


def roc(prices: list[float], period: int = 5) -> float | None:
    """Rate of Change (가격 변화율).

    Returns:
        변화율 (%). 양수=상승 모멘텀, 음수=하락 모멘텀.
    """
    if len(prices) < period + 1:
        return None
    old = prices[-period - 1]
    if old == 0:
        return None
    return (prices[-1] - old) / old * 100


def bollinger_bands(
    prices: list[float], period: int = 20, num_std: float = 2.0
) -> tuple[float, float, float] | None:
    """볼린저 밴드.

    Returns:
        (upper, middle, lower) 또는 데이터 부족 시 None.
    """
    if len(prices) < period:
        return None

    middle = sma(prices, period)
    if middle is None:
        return None

    recent = prices[-period:]
    variance = sum((p - middle) ** 2 for p in recent) / period
    std = variance**0.5

    upper = middle + num_std * std
    lower = middle - num_std * std
    return upper, middle, lower


def macd(
    prices: list[float],
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> tuple[float, float, float] | None:
    """MACD (Moving Average Convergence Divergence).

    Returns:
        (macd_line, signal_line, histogram) 또는 데이터 부족 시 None.
    """
    if len(prices) < slow + signal_period:
        return None

    macd_values = []
    for i in range(slow, len(prices) + 1):
        subset = prices[:i]
        fast_ema = ema(subset, fast)
        slow_ema = ema(subset, slow)
        if fast_ema is not None and slow_ema is not None:
            macd_values.append(fast_ema - slow_ema)

    if len(macd_values) < signal_period:
        return None

    signal_line = ema(macd_values, signal_period)
    if signal_line is None:
        return None

    macd_line = macd_values[-1]
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def stochastic(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    k_period: int = 14,
    d_period: int = 3,
) -> tuple[float, float] | None:
    """스토캐스틱 오실레이터.

    Returns:
        (%K, %D) 또는 데이터 부족 시 None.
    """
    if len(closes) < k_period + d_period - 1:
        return None

    k_values = []
    for i in range(k_period - 1, len(closes)):
        highest = max(highs[i - k_period + 1 : i + 1])
        lowest = min(lows[i - k_period + 1 : i + 1])
        if highest == lowest:
            k_values.append(50.0)
        else:
            k_values.append((closes[i] - lowest) / (highest - lowest) * 100)

    if len(k_values) < d_period:
        return None

    k = k_values[-1]
    d = sum(k_values[-d_period:]) / d_period
    return k, d
