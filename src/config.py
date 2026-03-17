"""앱 설정 관리 모듈."""

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / "config.json"


@dataclass
class TossConfig:
    """토스증권 로그인 및 브라우저 설정."""

    login_url: str = "https://tossinvest.com"
    headless: bool = False
    slow_mo: int = 100  # ms, 브라우저 동작 딜레이
    timeout: int = 30000  # ms


@dataclass
class TradingConfig:
    """매매 관련 설정."""

    watchlist: list[str] = field(default_factory=list)  # 감시 종목 코드 리스트
    check_interval: int = 10  # 초, 시세 확인 주기
    max_buy_amount: int = 0  # 1회 최대 매수 금액 (0=무제한)
    dry_run: bool = True  # True이면 실제 주문 안 함


@dataclass
class DashboardConfig:
    """대시보드 설정."""

    host: str = "127.0.0.1"
    port: int = 8080


@dataclass
class AppConfig:
    """전체 앱 설정."""

    toss: TossConfig = field(default_factory=TossConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)

    def save(self, path: Path | None = None) -> None:
        target = path or CONFIG_PATH
        target.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False))

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        target = path or CONFIG_PATH
        if not target.exists():
            cfg = cls()
            cfg.save(target)
            return cfg
        data = json.loads(target.read_text())
        return cls(
            toss=TossConfig(**data.get("toss", {})),
            trading=TradingConfig(**data.get("trading", {})),
            dashboard=DashboardConfig(**data.get("dashboard", {})),
        )
