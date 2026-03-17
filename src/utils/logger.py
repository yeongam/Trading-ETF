"""로깅 설정."""

import logging
import sys


def setup_logger(level: str = "INFO") -> None:
    """앱 전체 로거를 설정합니다."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("trading.log", encoding="utf-8"),
        ],
    )
