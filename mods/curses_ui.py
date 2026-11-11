"""
Curses split-screen UI for una build.
Top 50%: una output (stdout/stderr)
Bottom 50%: build logs monitor
"""

import curses
import os
import sys
import threading
import time
import re
import queue
from pathlib import Path

# For wrapping and width calculations
try:
    from wcwidth import wcwidth
    HAVE_WCWIDTH = True
except Exception:
    HAVE_WCWIDTH = False

# Try to import optional helper if present
try:
    from bld.wrap_utils import wrap_ansi, visible_width, split_ansi
    HAVE_WRAP_UTILS = True
except Exception:
    HAVE_WRAP_UTILS = False

# Regex to remove CSI and other ANSI control sequences for bottom pane
CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# Matches ESC followed by '(' or ')' sequences like ESC ( B
ESC_TWO_RE = re.compile(r"\x1b[\(\)][ -/]*[@-~]")
# Catch-all: ESC followed by a single char (used for bottom cleaning)
ESC_CHAR_RE = re.compile(r"\x1b.")
CONTROL_RE_PLAIN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# For Writer: regex to remove CSI sequences that are NOT SGR (i.e., not ending with 'm')
NON_SGR_CSI_RE = re.compile(r"\x1b\[(?![0-9;]*m)[0-9;?]*[ -/]*[@-~]")


def clean_log_text(data):
    """Strip ANSI escape sequences and non-printable control characters."""
    if not data:
        return ""
    clean = CSI_RE.sub("", data)
    clean = ESC_TWO_RE.sub("", clean)
    clean = ESC_CHAR_RE.sub("", clean)
    clean = CONTROL_RE_PLAIN.sub("", clean)
    return clean


class Writer:
    """File-like object that writes formatted text to a curses window."""

    ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
    CONTROL_RE = re.compile(r"[\r\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f]")

    def __init__(self, win, lock=None):
        self.win = win
        self.lock = lock or threading.RLock()
        try:
            h, w = self.win.getmaxyx()
            start_line = 2 if h > 2 else max(0, h - 1)
            self.win.move(start_line, 0)
            self.win.noutrefresh()
        except Exception:
            try:
                self.win.move(0, 0)
                self.win.noutrefresh()
            except Exception:
                pass

    def isatty(self):
        """Return False since this is not a real terminal."""
        return False

    def flush(self):
        """Flush output - refresh the curses window."""
        with self.lock:
            try:
                self.win.noutrefresh()
                curses.doupdate()
            except Exception:
                pass

    def write(self, text):
        with self.lock:
            # Remove control characters except \r
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
                try:
                    t = re.sub(r"\[$", "", t)
                except Exception:
                    pass

                for i, sgr in enumerate(sgrs):
                    t = t.replace(f"__SGR_{i}__", sgr)

                text = t
            except Exception:
                pass

            try:
                h, w = self.win.getmaxyx()

                parts = []
                last_index = 0
                for m in self.ANSI_RE.finditer(text):
                    if m.start() > last_index:
                        parts.append((text[last_index:m.start()], None))
                    parts.append((None, m.group(0)))
                    last_index = m.end()
                if last_index < len(text):
                    parts.append((text[last_index:], None))

                attr = 0
                bold = False
                buf = ""

                def flush_buf(_y, buf, attr):
                    try:
                        if not buf:
                            return
                        lines = buf.split("\n")
                        for idx, line in enumerate(lines):
                            if '\r' in line:
                                line = line.split('\r')[-1]
                            y, x = self.win.getyx()
                            if line:
                                truncated = line[:max(w - 1, 0)]
                                try:
                                    self.win.addstr(y, 0, truncated, attr)
                                    self.win.clrtoeol()
                                except Exception:
                                    pass
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
                            part = ESC_CHAR_RE.sub("", part)
                        except Exception:
                            pass
                        buf += part
                    else:
                        flush_buf(self.win.getyx()[0], buf, curses.A_BOLD | attr if bold else attr)
                        buf = ""
                        nums = code.lstrip('\x1b[').rstrip('m')
                        nums_list = [int(n) for n in nums.split(';') if n.isdigit()]
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

                flush_buf(self.win.getyx()[0], buf, curses.A_BOLD | attr if bold else attr)

                try:
                    self.win.noutrefresh()
                except Exception:
                    pass
            except Exception:
                pass
        return len(text)


class QueueWriter:
    """Non-blocking, unbuffered writer that routes sys.stdout/stderr to CursesUI."""

    def __init__(self, ui):
        self.ui = ui

    def write(self, text):
        if text:
            crlf_text = re.sub(r'(?<!\r)\n', '\r\n', text)
            self.ui.write_top(crlf_text)
        return len(text) if text else 0

    def flush(self):
        pass

    def isatty(self):
        return False


class CursesUI:
    """Manages a 50/50 split curses display using non-blocking, unbuffered event-driven I/O."""

    def __init__(self, log_dir=None):
        self.log_dir = log_dir
        self.stdscr = None
        self.top = None
        self.bottom = None
        self.h = 0
        self.w = 0
        self.sep_row = 0
        self.running = False
        self.top_lock = threading.RLock()   # Retained for backwards compatibility
        self.bottom_lock = threading.RLock()
        self.old_stdout = None
        self.old_stderr = None
        self.log_thread = None              # No background polling watcher thread needed
        self.build_thread = None
        self.build_func = None
        self.build_args = None
        self.build_result = None
        self.event_queue = queue.Queue()
        self.top_queue = self.event_queue   # Retained for backwards compatibility
        self.current_log_queue = queue.Queue()  # Retained for backwards compatibility
        self.separator_text = "Ready"
        self.current_log_file = None
        self.bottom_buffer = []
        self.bottom_remainder = ""
        self.writer = None

    def write_top(self, text):
        """Non-blocking, unbuffered write to top window."""
        if text:
            self.event_queue.put_nowait(("top", text))

    def write_bottom(self, data):
        """Non-blocking, unbuffered streaming write to bottom window."""
        if data:
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace")
            self.event_queue.put_nowait(("bottom", data))

    def set_status(self, text):
        """Update the middle separator status label (non-blocking)."""
        if text is not None:
            self.event_queue.put_nowait(("status", str(text)))

    def set_current_log(self, log_path):
        """Notify UI of current log file and clear bottom pane for the new component."""
        if log_path:
            self.event_queue.put_nowait(("log_switch", str(log_path)))

    def start(self, build_func, *args, **kwargs):
        """Start curses UI and run build_func in background. Returns build result."""
        self.build_func = build_func
        self.build_args = (args, kwargs)
        try:
            curses.wrapper(self._main)
        except Exception as e:
            try:
                curses.echo()
                curses.nocbreak()
                curses.endwin()
            except Exception:
                pass
            raise e
        return self.build_result

    def _setup_windows(self, color1=0):
        """Compute window geometry and initialize subwindows."""
        min_h = 3
        sep_h = 1
        top_h = max(min_h, self.h // 2)
        bot_h = self.h - top_h - sep_h
        if bot_h < min_h:
            top_h = max(min_h, self.h - sep_h - min_h)
            bot_h = self.h - top_h - sep_h
        if bot_h < 1:
            bot_h = max(1, self.h - top_h - sep_h)

        self.top = curses.newwin(top_h, self.w, 0, 0)
        self.sep_row = top_h
        self.bottom = curses.newwin(bot_h, self.w, top_h + sep_h, 0)

        self.top.scrollok(True)
        self.bottom.scrollok(True)
        try:
            self.bottom.setscrreg(0, bot_h - 1)
        except Exception:
            pass

        try:
            self.top.addstr(0, 0, "=== Una Output ===", curses.A_BOLD | color1)
            self.top.noutrefresh()
        except Exception:
            pass

        self._draw_separator(self.separator_text)

        try:
            self.bottom.clear()
            self.bottom.noutrefresh()
        except Exception:
            pass

        try:
            curses.doupdate()
        except Exception:
            pass

    def _draw_separator(self, text=None):
        """Draw middle separator line with centered status text."""
        if self.stdscr is None:
            return
        if text is not None:
            self.separator_text = str(text)
        label = f" {self.separator_text} "
        total = self.w
        left_pad = max(0, (total - len(label)) // 2)
        right_pad = max(0, total - left_pad - len(label))
        line = "─" * left_pad + label + "─" * right_pad
        try:
            self.stdscr.addstr(self.sep_row, 0, line[:total], curses.A_BOLD)
            self.stdscr.noutrefresh()
        except Exception:
            pass

    def _append_bottom_chunk(self, chunk):
        """Process incoming log chunk, wrap lines, and append to bottom buffer."""
        if not chunk:
            return
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", errors="replace")
        clean = clean_log_text(chunk)
        if not clean:
            return

        bot_h, bot_w = self.bottom.getmaxyx()
        max_cols = max(bot_w - 1, 1)

        combined = self.bottom_remainder + clean
        lines = combined.split("\n")
        self.bottom_remainder = lines.pop()

        for raw_line in lines:
            if '\r' in raw_line:
                raw_line = raw_line.split('\r')[-1]
            if not raw_line:
                self.bottom_buffer.append("")
                continue

            if HAVE_WRAP_UTILS and HAVE_WCWIDTH:
                try:
                    wrapped = wrap_ansi(raw_line, max_cols)
                except Exception:
                    wrapped = [raw_line[i:i + max_cols] for i in range(0, len(raw_line), max_cols)]
            else:
                wrapped = [raw_line[i:i + max_cols] for i in range(0, len(raw_line), max_cols)]

            self.bottom_buffer.extend(wrapped)

        content_h = max(0, bot_h)
        if content_h > 0 and len(self.bottom_buffer) > content_h:
            self.bottom_buffer = self.bottom_buffer[-content_h:]

    def _render_bottom(self):
        """Redraw the bottom window content area."""
        try:
            bot_h, bot_w = self.bottom.getmaxyx()
            max_cols = max(bot_w - 1, 1)
            content_h = max(0, bot_h)

            display_lines = list(self.bottom_buffer)
            if self.bottom_remainder:
                rem = self.bottom_remainder
                if '\r' in rem:
                    rem = rem.split('\r')[-1]
                if rem:
                    display_lines.append(rem[:max_cols])
                    if len(display_lines) > content_h:
                        display_lines = display_lines[-content_h:]

            for row in range(content_h):
                if row < len(display_lines):
                    line = display_lines[row]
                    try:
                        self.bottom.addnstr(row, 0, line, max_cols)
                    except Exception:
                        try:
                            self.bottom.addstr(row, 0, line[:max_cols])
                        except Exception:
                            pass
                else:
                    try:
                        self.bottom.move(row, 0)
                    except Exception:
                        pass
                self.bottom.clrtoeol()

            self.bottom.noutrefresh()
        except Exception:
            pass

    def _main(self, stdscr):
        self.stdscr = stdscr
        curses.noecho()
        curses.cbreak()
        try:
            curses.curs_set(0)
        except Exception:
            pass
        try:
            stdscr.clear()
            stdscr.refresh()
        except Exception:
            pass

        curses.start_color()
        color1 = 0
        if curses.has_colors():
            try:
                curses.use_default_colors()
            except Exception:
                pass
            try:
                curses.init_pair(1, curses.COLOR_GREEN, -1)
                curses.init_pair(2, curses.COLOR_CYAN, -1)
                color1 = curses.color_pair(1)
            except Exception:
                pass

        self.h, self.w = stdscr.getmaxyx()
        self._setup_windows(color1)

        self.running = True

        # Close stdin so no child process inherits a terminal stdin
        try:
            devnull_fd = os.open(os.devnull, os.O_RDONLY)
            os.dup2(devnull_fd, 0)
            os.close(devnull_fd)
        except Exception:
            pass

        # Redirect sys.stdout and sys.stderr to QueueWriter
        self.old_stdout = sys.stdout
        self.old_stderr = sys.stderr
        self.writer = Writer(self.top, self.top_lock)
        sys.stdout = QueueWriter(self)
        sys.stderr = sys.stdout

        # Start build function in single background thread
        if self.build_func:
            args_tuple, kwargs_dict = self.build_args

            def run_build_wrapper():
                try:
                    self.build_result = self.build_func(*args_tuple, **kwargs_dict)
                except Exception:
                    self.build_result = False
                finally:
                    self.event_queue.put(("done", self.build_result))

            self.build_thread = threading.Thread(
                target=run_build_wrapper, daemon=True
            )
            self.build_thread.start()

        # Run single-threaded event loop for all curses rendering
        self._ui_event_loop()

        self.close()

    def _ui_event_loop(self):
        """Single-threaded event loop. Only this loop invokes curses calls."""
        last_log_check = 0.0
        last_log_pos = 0

        while self.running:
            events = []
            try:
                event = self.event_queue.get(timeout=0.02)
                events.append(event)
                while True:
                    try:
                        events.append(self.event_queue.get_nowait())
                    except queue.Empty:
                        break
            except queue.Empty:
                pass

            top_dirty = False
            bottom_dirty = False
            status_dirty = False

            for ev in events:
                if not ev:
                    continue
                if isinstance(ev, str):
                    ev_type, ev_val = "top", ev
                elif isinstance(ev, tuple):
                    ev_type = ev[0]
                    ev_val = ev[1] if len(ev) > 1 else None
                else:
                    continue

                if ev_type == "top":
                    if ev_val and self.writer:
                        try:
                            self.writer.write(ev_val)
                            top_dirty = True
                        except Exception:
                            pass

                elif ev_type == "bottom":
                    if ev_val:
                        self._append_bottom_chunk(ev_val)
                        bottom_dirty = True

                elif ev_type == "status":
                    self.separator_text = str(ev_val)
                    status_dirty = True

                elif ev_type == "log_switch":
                    self.current_log_file = ev_val
                    last_log_pos = 0
                    self.bottom_buffer.clear()
                    self.bottom_remainder = ""
                    try:
                        stem = Path(ev_val).stem if ev_val else "..."
                        self.separator_text = stem
                    except Exception:
                        pass
                    status_dirty = True
                    bottom_dirty = True

                elif ev_type == "done":
                    self.running = False
                    break

            # Non-blocking fallback for log files written directly without streaming
            now = time.monotonic()
            if self.current_log_file and (now - last_log_check > 0.1):
                last_log_check = now
                try:
                    p = Path(self.current_log_file)
                    if p.exists():
                        cur_size = p.stat().st_size
                        if cur_size > last_log_pos:
                            fd = os.open(str(p), os.O_RDONLY | os.O_NONBLOCK)
                            try:
                                os.lseek(fd, last_log_pos, os.SEEK_SET)
                                data = os.read(fd, 65536)
                                last_log_pos = os.lseek(fd, 0, os.SEEK_CUR)
                                if data:
                                    self._append_bottom_chunk(data.decode("utf-8", errors="replace"))
                                    bottom_dirty = True
                            finally:
                                os.close(fd)
                        elif cur_size < last_log_pos:
                            last_log_pos = 0
                except Exception:
                    pass

            # Update windows in batch
            if bottom_dirty:
                self._render_bottom()
            if status_dirty:
                self._draw_separator()
            if top_dirty and self.top:
                try:
                    self.top.noutrefresh()
                except Exception:
                    pass

            if top_dirty or bottom_dirty or status_dirty:
                try:
                    curses.doupdate()
                except Exception:
                    pass

    def close(self):
        """Restore terminal and standard streams."""
        self.running = False

        if self.old_stdout:
            sys.stdout = self.old_stdout
        if self.old_stderr:
            sys.stderr = self.old_stderr

        if self.build_thread and self.build_thread.is_alive():
            try:
                self.build_thread.join(timeout=1.0)
            except Exception:
                pass

        try:
            curses.echo()
            curses.nocbreak()
            curses.endwin()
        except Exception:
            pass
