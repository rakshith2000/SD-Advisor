"""Logging configuration.

Structured-ish line logging to both stdout (captured by systemd) and a rotating
file. Every log line carries the component so a digest failure is separable
from a sync failure when reading a week of logs.
"""

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

_configured = False

LOG_FORMAT = '%(asctime)s %(levelname)-7s [%(name)s] %(message)s'
DATE_FORMAT = '%Y-%m-%d %H:%M:%S'


def setup_logging(log_dir: Optional[Path] = None, level: str = 'INFO') -> None:
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if log_dir is not None:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.TimedRotatingFileHandler(
            log_dir / 'advisor.log', when='midnight', backupCount=30, encoding='utf-8'
        )
        rotating.setFormatter(formatter)
        root.addHandler(rotating)

    # These are chatty and drown out our own lines.
    for noisy in ('urllib3', 'httpx', 'httpcore', 'openai', 'apscheduler.executors'):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
