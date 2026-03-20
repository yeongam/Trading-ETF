"""브로커 추상 베이스 클래스."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class StockPrice:
    """종목 시세 정보."""

    code: str
    name: str
    current_price: int
    change_rate: float  # 등락률 (%)
    volume: int
    is_etf: bool = False  # ETF 여부 (토스: 종목명이 영문 티커만 → ETF)


@dataclass
class OrderResult:
    """주문 결과."""

    success: bool
    order_id: str | None = None
    message: str = ""


@dataclass
class Position:
    """보유 종목 정보."""

    code: str
    name: str
    quantity: int
    avg_price: int
    current_price: int

    @property
    def profit_rate(self) -> float:
        if self.avg_price == 0:
            return 0.0
        return ((self.current_price - self.avg_price) / self.avg_price) * 100

    @property
    def profit_amount(self) -> int:
        return (self.current_price - self.avg_price) * self.quantity


class BaseBroker(ABC):
    """증권사 브로커 인터페이스."""

    @abstractmethod
    async def login(self) -> bool:
        """로그인. 성공 시 True 반환."""

    @abstractmethod
    async def get_balance(self) -> int:
        """예수금 조회."""

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """보유 종목 목록 조회."""

    @abstractmethod
    async def get_price(self, code: str) -> StockPrice | None:
        """종목 현재가 조회."""

    @abstractmethod
    async def buy(self, code: str, quantity: int, price: int = 0) -> OrderResult:
        """매수 주문. price=0 이면 시장가."""

    @abstractmethod
    async def sell(self, code: str, quantity: int, price: int = 0) -> OrderResult:
        """매도 주문. price=0 이면 시장가."""

    @abstractmethod
    async def close(self) -> None:
        """브라우저/세션 종료."""
