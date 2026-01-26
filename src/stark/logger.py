import logging

class StarkLogger:

    # ANSI Color Codes
    RESET = "\033[0m"
    BOLD = "\033[1m"
    GRAY = "\033[90m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"

    class LevelOnlyFormatter(logging.Formatter):
        """Formatter that only applies color to the levelname."""

        # Color codes
        grey = "\x1b[38;20m"
        cyan = "\x1b[36;20m"
        yellow = "\x1b[33;20m"
        red = "\x1b[31;20m"
        bold_red = "\x1b[31;1m"
        file_color = "\x1b[35m"
        reset = "\x1b[0m"

        # Base format - we use a placeholder {color} for the level part
        # Note: We move the color start/end to be tight around %(levelname)s
        BASE_FORMAT = "%(asctime)s - {color}%(levelname)-8s{reset} - %(message)s {file_color}(%(filename)s:%(lineno)d){reset}"

        FORMATS = {
            logging.DEBUG: BASE_FORMAT.format(color=grey, reset=reset, file_color=file_color),
            logging.INFO: BASE_FORMAT.format(color=cyan, reset=reset, file_color=file_color),
            logging.WARNING: BASE_FORMAT.format(color=yellow, reset=reset, file_color=file_color),
            logging.ERROR: BASE_FORMAT.format(color=red, reset=reset, file_color=file_color),
            logging.CRITICAL: BASE_FORMAT.format(color=bold_red, reset=reset, file_color=file_color),
        }

        def format(self, record):
            log_fmt = self.FORMATS.get(record.levelno)
            formatter = logging.Formatter(log_fmt, datefmt='%Y-%m-%d %H:%M:%S')
            return formatter.format(record)

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.DEBUG)
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(logging.DEBUG)
        stream_handler.setFormatter(self.LevelOnlyFormatter())
        self.logger.addHandler(stream_handler)
    
    def get_logger(self) -> logging.Logger:
        return self.logger


logger: logging.Logger = StarkLogger().get_logger()