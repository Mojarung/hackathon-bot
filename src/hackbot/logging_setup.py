"""Logging configuration. Chatty third-party loggers are muted deliberately."""

from __future__ import annotations

import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    for noisy, lvl in (
        ("aiogram.event", logging.WARNING),
        ("aiohttp.access", logging.WARNING),
        ("httpx", logging.WARNING),
        ("httpx2", logging.WARNING),
        ("openai", logging.WARNING),
        ("uvicorn.access", logging.WARNING),
        ("apscheduler", logging.WARNING),
    ):
        logging.getLogger(noisy).setLevel(lvl)
