"""
Curses split-screen UI for una build.
Top 50%: una output (stdout/stderr)
Bottom 50%: build logs monitor
"""

import curses
import sys
import threading
import time
import re
from pathlib import Path

# Regex to remove CSI and other ANSI control sequences for bottom pane
CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# Matches ESC followed by '(' or ')' sequences like ESC ( B
ESC_TWO_RE = re.compile(r"\x1b[\(\)][ -/]*[@-~]")
# Catch-all: ESC followed by a single char (used for bottom cleaning)
ESC_CHAR_RE = re.compile(r"\x1b.")
CONTROL_RE_PLAIN = re.compile(r"[\x00-\x1f\x7f]")
# For Writer: regex to remove CSI sequences that are NOT SGR (i.e., not ending with 'm')
NON_SGR_CSI_RE = re.compile(r"\x1b\[(?![0-9;]*m)[0-9;?]*[ -/]*[@-~]")


class Writer:
    """File-like object that writes to a curses window."""

    ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
    # Preserve ESC (\x1b) so ANSI sequences remain for parsing
    CONTROL_RE = re.compile(r"[\r\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f]")

    def __init__(self, win, lock):
        self.win = win
        self.lock = lock
        # Start at line 2 (after header) if window tall enough, otherwise put at last line
        try:
            h, w = self.win.getmaxyx()
            start_line = 2 if h > 2 else max(0, h - 1)
            self.win.move(start_line, 0)
            self.win.refresh()
        except Exception:
            try:
                self.win.move(0, 0)
                self.win.refresh()
            except Exception:
                pass

    def isatty(self):
        """Return False since this is not a real terminal."""
        return False

    def flush(self):
        """Flush output - refresh the curses window."""
        try:
            with self.lock:
                self.win.refresh()
        except Exception:
            pass

    def write(self, text):
        with self.lock:
            # Remove control characters except \r (needed for carriage return handling)
            text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f]", "", text)

            # Protect SGR (\x1b[...m) sequences, strip all other CSI/ESC, then restore SGR
            try:
                sgrs = []
                new_parts = []
                last = 0
                for i, m in enumerate(self.ANSI_RE.finditer(text)):
                    sgrs.append(m.group(0))
                    new_parts.append(text[last:m.start()])
                    new_parts.append(f"__SGR_{i}__")
                    last = m.end()
                new_parts.append(text[last:])
                t = "".join(new_parts)

                # Remove all remaining CSI/ESC sequences (non-SGR)
                try:
                    t = CSI_RE.sub("", t)
                except Exception:
                    pass
                try:
                    t = ESC_TWO_RE.sub("", t)
                except Exception:
                    pass
                try:
                    t = ESC_CHAR_RE.sub("", t)
                except Exception:
                    pass

                # Remove isolated '[' only if it's at the very end (incomplete CSI sequence)
                try:
                    t = re.sub(r"\[\Z", "", t)
                except Exception:
                    pass

                # Restore SGR tokens
                for i, sgr in enumerate(sgrs):
                    t = t.replace(f"__SGR_{i}__", sgr)

                text = t
            except Exception:
                pass

            try:
                h, w = self.win.getmaxyx()

                # We'll parse ANSI color codes and apply simple mappings to curses color pairs
                parts = []
                last_index = 0
                for m in self.ANSI_RE.finditer(text):
                    if m.start() > last_index:
                        parts.append((text[last_index:m.start()], None))
                    parts.append((None, m.group(0)))
                    last_index = m.end()
                if last_index < len(text):
                    parts.append((text[last_index:], None))

                # Current attribute
                attr = 0
                bold = False

                buf = ""

                def flush_buf(_y, buf, attr):
                    try:
                        if not buf:
                            return
                        # handle lines in buf
                        lines = buf.split("\n")
                        for idx, line in enumerate(lines):
                            y, x = self.win.getyx()
                            if line:
                                truncated = line[: max(w - 1, 0)]
                                try:
                                    self.win.addstr(y, 0, truncated, attr)
                                    self.win.clrtoeol()
                                except Exception:
                                    pass
                            # Always advance to next line after \n, unless it's the last empty line
                            if idx < len(lines) - 1:
                                y, x = self.win.getyx()
                                next_line = y + 1
                                if next_line < h:
                                    try:
                                        self.win.move(next_line, 0)
                                    except Exception:
                                        pass
                                else:
                                    try:
                                        self.win.scroll()
                                        self.win.move(h - 1, 0)
                                    except Exception:
                                        pass
                    except Exception:
                        pass

                for part, code in parts:
                    if code is None:
                        try:
                            # remove any leftover ESC+char sequences that are not SGR (these can be cursor controls)
                            part = ESC_CHAR_RE.sub("", part)
                        except Exception:
                            pass
                        buf += part
                    else:
                        # flush current buffer before processing code
                        flush_buf(self.win.getyx()[0], buf, curses.A_BOLD | attr if bold else attr)
                        buf = ""
                        # parse code numbers
                        nums = code.lstrip('\x1b[').rstrip('m')
                        nums_list = [int(n) for n in nums.split(';') if n.isdigit()]
                        # reset
                        if not nums_list or 0 in nums_list:
                            attr = 0
                            bold = False
                        else:
                            for n in nums_list:
                                if n == 1:
                                    bold = True
                                elif n in (32, 92):
                                    try:
                                        attr = curses.color_pair(1)
                                    except Exception:
                                        attr = 0
                                elif n in (36, 96):
                                    try:
                                        attr = curses.color_pair(2)
                                    except Exception:
                                        attr = 0
                                # ignore others
                # flush remainder
                flush_buf(self.win.getyx()[0], buf, curses.A_BOLD | attr if bold else attr)

                try:
                    self.win.refresh()
                except Exception:
                    pass
            except Exception:
                pass
        return len(text)

    def flush(self):
        with self.lock:
            try:
                self.win.refresh()
            except Exception:
                pass


class CursesUI:
    """Manages a 50/50 split curses display."""

    def __init__(self, log_dir=None):
        self.log_dir = log_dir
        self.stdscr = None
        self.top = None
        self.bottom = None
        self.h = 0
        self.w = 0
        self.running = False
        self.lock = threading.Lock()
        self.old_stdout = None
        self.old_stderr = None
        self.log_thread = None
        self.build_thread = None
        self.build_func = None
        self.build_args = None

    def start(self, build_func, *args, **kwargs):
        """Start curses UI and run build_func in background."""
        self.build_func = build_func
        self.build_args = (args, kwargs)
        try:
            curses.wrapper(self._main)
        except Exception as e:
            # Try to restore terminal before re-raising
            try:
                curses.echo()
                curses.endwin()
            except:
                pass
            raise e

    def _main(self, stdscr):
        self.stdscr = stdscr
        curses.noecho()
        curses.cbreak()
        stdscr.keypad(True)
        # Clear the screen to remove any prior terminal output and avoid overlap
        try:
            stdscr.clear()
            stdscr.refresh()
        except Exception:
            pass
        curses.start_color()
        has_colors = curses.has_colors()
        if has_colors:
            try:
                curses.use_default_colors()
            except Exception:
                pass
            curses.init_pair(1, curses.COLOR_GREEN, -1)
            curses.init_pair(2, curses.COLOR_CYAN, -1)
            color1 = curses.color_pair(1)
            color2 = curses.color_pair(2)
        else:
            color1 = 0
            color2 = 0

        self.h, self.w = stdscr.getmaxyx()

        # Calculate window heights - split with a 1-line separator
        min_h = 3
        sep_h = 1
        top_h = max(min_h, self.h // 2)
        bot_h = self.h - top_h - sep_h
        if bot_h < min_h:
            top_h = max(min_h, self.h - sep_h - min_h)
            bot_h = self.h - top_h - sep_h
        if bot_h < 1:
            bot_h = max(1, self.h - top_h - sep_h)

        # Create windows with a separator row between them
        self.top = curses.newwin(top_h, self.w, 0, 0)
        # Separator will be drawn on stdscr at row = top_h
        self.bottom = curses.newwin(bot_h, self.w, top_h + sep_h, 0)

        self.top.scrollok(True)
        self.bottom.scrollok(True)

        # Draw header on top window
        try:
            self.top.addstr(0, 0, "=== Una Output ===", curses.A_BOLD | color1)
            self.top.hline(1, 0, curses.ACS_HLINE, self.w)
        except Exception:
            pass
        self.top.refresh()

        # Draw separator line on the root window
        try:
            stdscr.hline(top_h, 0, curses.ACS_HLINE, self.w)
            stdscr.refresh()
        except Exception:
            pass

        # Draw headers on bottom window
        try:
            self.bottom.addstr(0, 0, "=== Build Logs ===", curses.A_BOLD | color2)
            self.bottom.hline(1, 0, curses.ACS_HLINE, self.w)
        except Exception:
            pass
        self.bottom.refresh()

        self.running = True

        # Redirect stdout/stderr to top window
        self.old_stdout = sys.stdout
        self.old_stderr = sys.stderr

        writer = Writer(self.top, self.lock)
        sys.stdout = writer
        sys.stderr = writer

        # Start log watcher for bottom window
        if self.log_dir:
            self._start_watcher()

        # Start build in background thread
        if self.build_func:
            args_tuple, kwargs_dict = self.build_args
            self.build_thread = threading.Thread(
                target=lambda: self.build_func(*args_tuple, **kwargs_dict), daemon=True
            )
            self.build_thread.start()

        # Wait for build to complete or user presses 'q'
        try:
            if self.build_thread:
                while self.build_thread.is_alive():
                    ch = stdscr.getch()
                    if ch == ord("q"):
                        break
                    time.sleep(0.1)
        except Exception:
            pass
        finally:
            self.close()

    def _start_watcher(self):
        """Watch log directory for new logs."""

        def watcher():
            path = Path(self.log_dir)
            last_log = None
            last_pos = 0

            while self.running:
                try:
                    if not path.exists():
                        time.sleep(0.5)
                        continue

                    logs = sorted(
                        path.glob("*.txt"),
                        key=lambda p: p.stat().st_mtime,
                        reverse=True,
                    )

                    if logs:
                        cur = logs[0]
                        if cur != last_log:
                            last_log = cur
                            last_pos = 0

                            with self.lock:
                                # Clear bottom window and redraw header
                                self.bottom.clear()
                                self.bottom.addstr(
                                    0,
                                    0,
                                    "=== {} ===".format(cur.name),
                                    curses.A_BOLD | curses.color_pair(2),
                                )
                                self.bottom.hline(1, 0, curses.ACS_HLINE, self.w)
                                self.bottom.move(2, 0)
                                self.bottom.refresh()

                        if cur.exists():
                            with open(cur, "r") as f:
                                f.seek(last_pos)
                                data = f.read()
                                data = None
                                if data:
                                    # Normalize \r\n to \n, but preserve standalone \r for cursor positioning
                                    try:
                                        data = data.replace('\r\n', '\n')
                                    except Exception:
                                        pass
                                    # Strip ANSI/terminal control sequences for bottom pane
                                    clean = CSI_RE.sub("", data)
                                    clean = ESC_TWO_RE.sub("", clean)
                                    clean = ESC_CHAR_RE.sub("", clean)
                                    # Remove control chars except \r and \n
                                    clean = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f]", "", clean)
                                    with self.lock:
                                        h, w = self.bottom.getmaxyx()

                                        # Process text handling \r by moving cursor to column 0
                                        lines = clean.split("\n")
                                        for line in lines:
                                            y, x = self.bottom.getyx()

                                            # Split by \r and write each segment
                                            parts = line.split('\r')

                                            for i, part in enumerate(parts):
                                                if i > 0:
                                                    # \r moves cursor to column 0 on same line
                                                    self.bottom.move(y, 0)

                                                if part:
                                                    truncated = part[: w - 1]
                                                    try:
                                                        self.bottom.addstr(y, 0, truncated)
                                                        self.bottom.clrtoeol()
                                                    except Exception:
                                                        try:
                                                            self.bottom.addstr(y, 0, truncated)
                                                        except Exception:
                                                            pass

                                            # Move to next line after processing all \r-separated parts
                                            y, x = self.bottom.getyx()
                                            if y < h - 1:
                                                self.bottom.move(y + 1, 0)
                                            else:
                                                self.bottom.scroll()
                                                self.bottom.move(h - 1, 0)

                                        try:
                                            self.bottom.refresh()
                                        except Exception:
                                            pass
                                    last_pos = f.tell()
                except Exception:
                    pass
                time.sleep(0.2)

        self.log_thread = threading.Thread(target=watcher, daemon=True)
        self.log_thread.start()

    def close(self):
        """Restore terminal."""
        self.running = False

        if self.log_thread:
            self.log_thread.join(timeout=1)

        if self.old_stdout:
            sys.stdout = self.old_stdout
        if self.old_stderr:
            sys.stderr = self.old_stderr

        # curses.wrapper() handles cleanup, but we need to ensure terminal is restored
        try:
            curses.endwin()
        except Exception:
            pass
