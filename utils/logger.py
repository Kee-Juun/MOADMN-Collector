"""
Simple file+console logger for MOADMN collector.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path


class Logger:
    """Handles logging to both a run folder file and stdout."""

    def __init__(self, log_dir: str):
        path = Path(log_dir)
        self.log_dir = path if path.is_absolute() else (Path.home() / path)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = self.log_dir / f"Log_{timestamp}.txt"

        fmt = logging.Formatter(fmt="%(asctime)s - %(levelname)s - %(message)s", datefmt="%I:%M:%S %p")
        file_handler = logging.FileHandler(self.log_file, encoding="utf-8")
        file_handler.setFormatter(fmt)
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(fmt)

        self.logger = logging.getLogger("MOADMN")
        self.logger.setLevel(logging.INFO)
        if self.logger.handlers:
            self.logger.handlers.clear()
        self.logger.addHandler(file_handler)
        self.logger.addHandler(stream_handler)
        self.logger.propagate = False

    def info(self, message: str) -> None:
        self.logger.info(message)

    def warning(self, message: str) -> None:
        self.logger.warning(message)

    def error(self, message: str) -> None:
        self.logger.error(message)


