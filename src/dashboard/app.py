"""FastAPI 웹 대시보드 - 매매 모니터링 및 제어."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from ..config import AppConfig
from ..scheduler import TradingScheduler

app = FastAPI(title="토스증권 자동매매 대시보드")

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# 런타임에 main.py에서 주입
_scheduler: TradingScheduler | None = None
_config: AppConfig | None = None


def set_scheduler(scheduler: TradingScheduler) -> None:
    global _scheduler
    _scheduler = scheduler


def set_config(config: AppConfig) -> None:
    global _config
    _config = config


@app.get("/", response_class=HTMLResponse)
async def index():
    """메인 대시보드 페이지."""
    html_path = TEMPLATES_DIR / "index.html"
    return HTMLResponse(html_path.read_text())


@app.get("/api/status")
async def get_status():
    """현재 상태 조회."""
    return {
        "running": _scheduler.is_running if _scheduler else False,
        "dry_run": _config.trading.dry_run if _config else True,
        "watchlist": _config.trading.watchlist if _config else [],
        "check_interval": _config.trading.check_interval if _config else 0,
        "risk_stats": _scheduler.risk_stats if _scheduler else {},
        "total_pnl_amount": _scheduler.total_pnl_amount if _scheduler else 0,
        "total_budget": _config.trading.total_budget if _config else 100000,
    }


@app.get("/api/trades")
async def get_trades():
    """매매 이력 조회."""
    if not _scheduler:
        return {"trades": []}
    return {"trades": _scheduler.trade_log}


@app.post("/api/start")
async def start_scheduler():
    """스케줄러 시작."""
    import asyncio
    import logging
    _logger = logging.getLogger(__name__)
    if not _scheduler:
        _logger.error("스케줄러 미초기화 상태에서 시작 요청")
        return {"error": "스케줄러가 초기화되지 않았습니다."}
    if _scheduler.is_running:
        _logger.warning("이미 실행 중 — 시작 요청 무시")
        return {"message": "이미 실행 중입니다."}
    _logger.info("▶ 시작 버튼 클릭 → 스케줄러 태스크 생성")
    asyncio.create_task(_scheduler.start())
    return {"message": "스케줄러가 시작되었습니다."}


@app.post("/api/stop")
async def stop_scheduler():
    """스케줄러 중지."""
    if not _scheduler:
        return {"error": "스케줄러가 초기화되지 않았습니다."}
    _scheduler.stop()
    return {"message": "스케줄러가 중지되었습니다."}


@app.post("/api/run-once")
async def run_once():
    """전략 1회 수동 실행."""
    if not _scheduler:
        return {"error": "스케줄러가 초기화되지 않았습니다."}
    results = await _scheduler.run_once()
    return {"results": results}


@app.get("/api/positions")
async def get_positions():
    """보유 종목 조회 (브로커 연동)."""
    # 브로커가 연결되어 있을 때만 동작
    return {"positions": []}


@app.post("/api/config")
async def update_config(request: Request):
    """설정 업데이트."""
    if not _config:
        return {"error": "설정이 초기화되지 않았습니다."}
    import logging
    _logger = logging.getLogger(__name__)
    data = await request.json()
    if "watchlist" in data:
        _config.trading.watchlist = data["watchlist"]
    if "check_interval" in data:
        old_interval = _config.trading.check_interval
        new_interval = int(data["check_interval"])
        if old_interval != new_interval:
            _config.trading.check_interval = new_interval
            _logger.info("사이클 주기 변경: %d초 → %d초", old_interval, new_interval)
    if "dry_run" in data:
        old_dry = _config.trading.dry_run
        new_dry = bool(data["dry_run"])
        if old_dry != new_dry:
            _config.trading.dry_run = new_dry
            _logger.info("매매 모드 변경: %s → %s",
                         "모의매매" if old_dry else "실제매매",
                         "모의매매" if new_dry else "실제매매")
    if "max_buy_amount" in data:
        _config.trading.max_buy_amount = data["max_buy_amount"]
    if "total_budget" in data:
        new_budget = int(data["total_budget"])
        _config.trading.total_budget = new_budget
        # RiskManager에도 즉시 반영
        if _scheduler and _scheduler._risk:
            _scheduler._risk.update_budget(new_budget)
    _config.save()
    return {"message": "설정이 저장되었습니다."}
