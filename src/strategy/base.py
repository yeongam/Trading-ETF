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


class BaseStrategy(ABC):
    """매매 전략 베이스 클래스."""

    @property
    def name(self) -> str:
        return self.__class__.__name__

    @abstractmethod
    async def analyze(self, code: str, broker: BaseBroker) -> Signal:
        """종목을 분석하고 매매 신호를 반환합니다.

        Args:
            code: 종목 코드
            broker: 시세 조회에 사용할 브로커 인스턴스

        Returns:
            Signal: 매수/매도/홀드 신호
        """
