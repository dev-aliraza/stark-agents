import logging
import sys

LOGGER_NAME = "stark"

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATEFMT = "%H:%M:%S"


def _build_logger() -> logging.Logger:
    log = logging.getLogger(LOGGER_NAME)
    # Attach a handler so the CLI has output out of the box, but leave propagation on
    # so a host application's logging config (and pytest's caplog) still sees records.
    if not log.handlers and not logging.getLogger().handlers:
        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setFormatter(logging.Formatter(fmt=_FORMAT, datefmt=_DATEFMT))
        log.addHandler(handler)
    log.setLevel(logging.INFO)
    return log


logger: logging.Logger = _build_logger()


def configure_logging(level: int | str = logging.INFO) -> None:
    """Set the verbosity of the `stark` logger."""
    logger.setLevel(level)


def get_logger(suffix: str) -> logging.Logger:
    """Return a child logger, e.g. get_logger("mcp") -> "stark.mcp"."""
    return logging.getLogger(f"{LOGGER_NAME}.{suffix}")
