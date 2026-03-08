import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOGGER_NAME = "discord_bot"
DEFAULT_MAX_LOG_BYTES = 2 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 2
_configured = False


def setup_logging() -> logging.Logger:
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    if _configured and logger.handlers:
        return logger

    base_dir = Path(__file__).resolve().parent.parent
    log_dir = Path(os.getenv("BOT_LOG_DIR", str(base_dir / "logs")))
    log_dir.mkdir(parents=True, exist_ok=True)

    max_bytes_raw = os.getenv("BOT_LOG_MAX_BYTES", str(DEFAULT_MAX_LOG_BYTES))
    backup_count_raw = os.getenv("BOT_LOG_BACKUP_COUNT", str(DEFAULT_BACKUP_COUNT))
    try:
        max_bytes = max(256 * 1024, int(max_bytes_raw))
    except ValueError:
        max_bytes = DEFAULT_MAX_LOG_BYTES
    try:
        backup_count = max(1, int(backup_count_raw))
    except ValueError:
        backup_count = DEFAULT_BACKUP_COUNT

    log_file = log_dir / "bot.log"

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    _configured = True
    logger.info(
        "Logging initialized. file=%s max_bytes=%s backup_count=%s",
        log_file,
        max_bytes,
        backup_count,
    )
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    setup_logging()
    if not name or name == LOGGER_NAME:
        return logging.getLogger(LOGGER_NAME)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")
