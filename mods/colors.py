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
    # Handle leading newlines - they should be outside the brackets
    prefix = ""
    if isinstance(msg, str) and msg.startswith('\n'):
        prefix = '\n'
        msg = msg[1:]
    print(_color(f"{prefix}{msg}", "92", bold=True), flush=True)


def warn(msg):
    """Orange (Yellow) for warnings."""
    print(_color(msg, "33", bold=True), flush=True)


def error(msg):
    """Red for errors."""
    print(_color(msg, "91", bold=True), flush=True)
