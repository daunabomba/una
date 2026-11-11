import sys
import re


def _crlf(text):
    """Ensure CR is added before every line feed (LF -> CRLF)."""
    if text is None:
        return ""
    s = str(text)
    return re.sub(r'(?<!\r)\n', '\r\n', s)


def _color(text, color_code, bold=False):
    """Add color codes only if output is a terminal."""
    if not sys.stdout.isatty():
        return text
    bold_code = "\033[1m" if bold else ""
    return f"\033[{color_code}m{bold_code}{text}\033[0m"


def info(msg):
    """Bright green for informative messages about what stage is being run."""
    # Handle leading newlines - they should be outside the brackets
    prefix = ""
    if isinstance(msg, str) and msg.startswith('\n'):
        prefix = '\r\n'
        msg = msg[1:]
    formatted = _crlf(f"{prefix}{msg}")
    print(_color(formatted, "92", bold=True), end="\r\n", flush=True)


def warn(msg):
    """Orange (Yellow) for warnings."""
    formatted = _crlf(msg)
    print(_color(formatted, "33", bold=True), end="\r\n", flush=True)


def error(msg):
    """Red for errors."""
    formatted = _crlf(msg)
    print(_color(formatted, "91", bold=True), end="\r\n", flush=True)
