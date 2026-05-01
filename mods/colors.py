import sys

# Check if output is a terminal
IS_TTY = sys.stdout.isatty()


def _color(text, color_code, bold=False):
    """Add color codes only if output is a terminal."""
    if not IS_TTY:
        return text
    bold_code = "\033[1m" if bold else ""
    return f"\033[{color_code}m{bold_code}{text}\033[0m"


def info(msg):
    """Bright green for informative messages about what stage is being run."""
    print(_color(msg, "92", bold=True))


def warn(msg):
    """Orange (Yellow) for warnings."""
    print(_color(msg, "33", bold=True))


def error(msg):
    """Red for errors."""
    print(_color(msg, "91", bold=True))
