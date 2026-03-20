"""매매 전략 프레임워크.

새 전략을 만들려면 BaseStrategy를 상속하고 analyze() 메서드를 구현하세요.

예시:
    class MyStrategy(BaseStrategy):
        async def analyze(self, code, broker):
            price = await broker.get_price(code)
            if price and price.current_price < 10000:
                return Signal(SignalType.BUY, code, quantity=1, reason="저가 매수")
            return Signal(SignalType.HOLD, code)
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from ..broker.base import BaseBroker


class SignalType(Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass
class Signal:
    """전략이 반환하는 매매 신호."""

    type: SignalType
    code: str
    quantity: int = 0
    price: int = 0  # 0이면 시장가
    reason: str = ""
    stop_loss: float = 0  # 동적 손절가 (전략이 계산)
    take_profit: float = 0  # 동적 익절가 (전략이 계산)
    entry_strategy: str = ""  # 진입 전략 식별자 ("A" / "B")


class BaseStrategy(ABC):
    """매매 전략 베이스 클래스."""

    @property
    def name(self) -> str:
        return self.__class__.__name__

    @abstractmethod
    async def analyze(self, code: str, broker: BaseBroker, min_data: int = 20) -> Signal:
        """종목을 분석하고 매매 신호를 반환합니다.

        Args:
            code: 종목 코드
            broker: 시세 조회에 사용할 브로커 인스턴스
            min_data: 신호 생성에 필요한 최소 데이터 횟수

        Returns:
            Signal: 매수/매도/홀드 신호
        """

    def get_data_count(self, code: str) -> int:
        """종목의 현재 데이터 수집 횟수 반환."""
        return 0

    def advance_cycle(self) -> None:
        """스케줄러 사이클 1회 진행 (기본 no-op). 서브클래스에서 오버라이드."""
