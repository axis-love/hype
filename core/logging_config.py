"""Logging configuration for the news bot.

Call configure_logging() once at process startup.
All modules then use logging.getLogger(__name__) normally.
"""

from __future__ import annotations

import logging
import os


def configure_logging(*, process_name: str = "newsbot") -> None:
    """Set up structured logging with level from LOG_LEVEL env var.

    Defaults to INFO. Set LOG_LEVEL=DEBUG for verbose output.
    """
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(
        level=level,
        format=f"%(asctime)s [{process_name}] %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # Quieten noisy third-party loggers.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)