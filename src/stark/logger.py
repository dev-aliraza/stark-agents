import datetime

class logger:

    # ANSI Color Codes
    RESET = "\033[0m"
    BOLD = "\033[1m"
    GRAY = "\033[90m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"

    include_time = True
    stark_logo = False

    @classmethod
    def _get_timestamp(cls):
        if cls.include_time:
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return f"{cls.GRAY}[{now}]{cls.RESET} "
        return ""
    
    @classmethod
    def _stark_logo(cls):
        if cls.stark_logo:
            return f"{cls.GRAY}[STARK]{cls.RESET} "
        return ""

    @classmethod
    def info(cls, message):
        """Log an informational message in Green."""
        timestamp = cls._get_timestamp()
        stark_logo = cls._stark_logo()
        print(f"{stark_logo}{timestamp}{cls.GREEN}[INFO]{cls.RESET} {message}")

    @classmethod
    def warn(cls, message):
        """Log a warning message in Yellow."""
        timestamp = cls._get_timestamp()
        stark_logo = cls._stark_logo()
        print(f"{stark_logo}{timestamp}{cls.YELLOW}[WARN]{cls.RESET} {message}")

    @classmethod
    def error(cls, message):
        """Log an error message in Red."""
        timestamp = cls._get_timestamp()
        stark_logo = cls._stark_logo()
        print(f"{stark_logo}{timestamp}{cls.RED}[ERROR] {message}{cls.RESET}")

    @classmethod
    def debug(cls, message):
        """Log a debug message in Blue."""
        timestamp = cls._get_timestamp()
        stark_logo = cls._stark_logo()
        print(f"{stark_logo}{timestamp}{cls.BLUE}[DEBUG]{cls.RESET} {message}")
    
    @classmethod
    def success(cls, message):
        """Log a success message in Bold Green."""
        timestamp = cls._get_timestamp()
        stark_logo = cls._stark_logo()
        print(f"{stark_logo}{timestamp}{cls.BOLD}{cls.GREEN}[SUCCESS]{cls.RESET} {message}")