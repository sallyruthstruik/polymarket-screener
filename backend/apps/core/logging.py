import logging


def get_log_preview(value: str, *, limit: int = 100) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}..."


def should_log_progress(index: int, *, every: int) -> bool:
    return index == 1 or index % every == 0


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
