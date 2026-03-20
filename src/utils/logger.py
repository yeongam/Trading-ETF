"""로깅 설정."""

import logging
import sys


def setup_logger(level: str = "INFO") -> None:
    """앱 전체 로거를 설정합니다."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # 기존 핸들러 제거 후 새로 설정 (basicConfig 무시 문제 방지)
    root.handlers.clear()
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stdout_h = logging.StreamHandler(sys.stdout)
    stdout_h.setFormatter(fmt)
    root.addHandler(stdout_h)
    file_h = logging.FileHandler("trading.log", encoding="utf-8")
    file_h.setFormatter(fmt)
    root.addHandler(file_h)
