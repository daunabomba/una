try:
    from colorama import Fore, Style, init
    init(autoreset=True)
except ImportError:
    # Minimal fallback for environments where colorama is not installed
    class Fore:
        RED = "\033[31m"
        GREEN = "\033[32m"
        YELLOW = "\033[33m" # Orange-ish
        BLUE = "\033[34m"
        MAGENTA = "\033[35m"
        CYAN = "\033[36m"
        WHITE = "\033[37m"
        RESET = "\033[39m"
        
        LIGHTRED_EX = "\033[91m"
        LIGHTGREEN_EX = "\033[92m"
        LIGHTYELLOW_EX = "\033[93m"
        
    class Style:
        BRIGHT = "\033[1m"
        DIM = "\033[2m"
        NORMAL = "\033[22m"
        RESET_ALL = "\033[0m"

def info(msg):
    """Bright green for informative messages about what stage is being run."""
    print(f"{Fore.LIGHTGREEN_EX}{Style.BRIGHT}{msg}{Style.RESET_ALL}")

def warn(msg):
    """Orange (Yellow) for warnings."""
    print(f"{Fore.YELLOW}{Style.BRIGHT}{msg}{Style.RESET_ALL}")

def error(msg):
    """Red for errors."""
    print(f"{Fore.LIGHTRED_EX}{Style.BRIGHT}{msg}{Style.RESET_ALL}")
