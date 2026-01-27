import logging

class StarkLogger:

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.__get_stream_handler())

    def __get_stream_handler(self) -> logging.StreamHandler:
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(logging.DEBUG)
        stream_handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s - %(levelname)-8s - %(message)s    (%(filename)s:%(lineno)d)",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
        return stream_handler
    
    def get_logger(self) -> logging.Logger:
        return self.logger

logger: logging.Logger = StarkLogger().get_logger()