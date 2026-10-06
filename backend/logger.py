"""Daily file logging for the mobile field-test server."""

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def configure_app_logging(output_dir: Path) -> Path:
    """Attach one shared midnight-rotating handler without changing console output."""
    log_dir = Path(output_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    target = log_dir / "app.log"
    existing = None
    for name in ("backend", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        for handler in tuple(logger.handlers):
            if getattr(handler, "_gildongmu_daily_log", False):
                logger.removeHandler(handler)
                if Path(handler.baseFilename) == target:
                    existing = handler
                else:
                    handler.close()
    handler = existing or TimedRotatingFileHandler(
        target, when="midnight", interval=1, backupCount=0,
        encoding="utf-8", delay=True,
    )
    handler._gildongmu_daily_log = True
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    for name in ("backend", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.addHandler(handler)
        if name == "backend":
            logger.setLevel(logging.INFO)
    return target
