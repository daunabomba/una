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


class Writer:
    """File-like object that writes to a curses window."""

    ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
    CONTROL_RE = re.compile(r"[\r\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

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

    def write(self, text):
        with self.lock:
            # Remove control characters
            text = self.CONTROL_RE.sub("", text)

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

                def flush_buf(y, buf, attr):
                    try:
                        if not buf:
                            return
                        # handle lines in buf
                        for line in buf.split("\n"):
                            y, x = self.win.getyx()
                            if line:
                                truncated = line[: max(w - 1, 0)]
                                try:
                                    self.win.addstr(y, 0, truncated, attr)
                                    self.win.clrtoeol()
                                except Exception:
                                    try:
                                        self.win.addstr(y, 0, truncated)
                                        self.win.clrtoeol()
                                    except Exception:
                                        pass
                                # Move to next line
                                if y < h - 1:
                                    try:
                                        self.win.move(y + 1, 0)
                                    except Exception:
                                        pass
                                else:
                                    try:
                                        self.win.scroll()
                                        self.win.move(max(h - 2, 0), 0)
                                    except Exception:
                                        pass
                            else:
                                # Empty line
                                if y < h - 1:
                                    try:
                                        self.win.move(y + 1, 0)
                                    except Exception:
                                        pass
                                else:
                                    try:
                                        self.win.scroll()
                                        self.win.move(max(h - 2, 0), 0)
                                    except Exception:
                                        pass
                    except Exception:
                        pass

                for part, code in parts:
                    if code is None:
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
                                if data:
                                    with self.lock:
                                        h, w = self.bottom.getmaxyx()
                                        self.bottom.move(2, 0)

                                        for line in data.split("\n"):
                                            y, x = self.bottom.getyx()

                                            if line:
                                                truncated = line[: w - 1]
                                                self.bottom.addstr(y, 0, truncated)
                                                self.bottom.clrtoeol()

                                            # Move to next line
                                            if y < h - 1:
                                                self.bottom.move(y + 1, 0)
                                            else:
                                                self.bottom.scroll()
                                                self.bottom.move(h - 2, 0)

                                        self.bottom.refresh()
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
