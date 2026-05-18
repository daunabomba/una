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
                    t = re.sub(r"\[$", "", t)
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
                                truncated = line[:max(w - 1, 0)]
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
        self.top_lock = threading.Lock()
        self.bottom_lock = threading.Lock()
        self.old_stdout = None
        self.old_stderr = None
        self.log_thread = None
        self.build_thread = None
        self.build_func = None
        self.build_args = None
        self.build_result = None
        self.current_log_queue = queue.Queue()  # For tracking which log file is currently being built
        self.top_queue = queue.Queue()  # Queue for UI-thread delivery of stdout/stderr text
        self.separator_text = ""  # current text shown in middle separator

    def start(self, build_func, *args, **kwargs):
        """Start curses UI and run build_func in background. Returns build result."""
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
        return self.build_result

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
        self.sep_row = top_h  # Store separator row for set_status()
        self.bottom = curses.newwin(bot_h, self.w, top_h + sep_h, 0)

        self.top.scrollok(True)
        self.bottom.scrollok(True)
        # Restrict scrolling to full content area (no header rows)
        try:
            self.bottom.setscrreg(0, bot_h - 1)
        except Exception:
            # Not all curses implementations expose set scroll region; ignore if unavailable
            pass

        # Draw header on top window
        try:
            top_h_actual, top_w_actual = self.top.getmaxyx()
            self.top.addstr(0, 0, "=== Una Output ===", curses.A_BOLD | color1)
        except Exception:
            pass
        self.top.refresh()

        # Draw single separator line (center label will be set by set_status)
        try:
            try:
                self.set_status("Ready")
            except Exception:
                pass
        except Exception:
            pass

        # Bottom window has no header to avoid corrupting content; leave it clear
        try:
            self.bottom.clear()
        except Exception:
            pass
        try:
            self.bottom.refresh()
        except Exception:
            pass

        self.running = True

        # Redirect stdout/stderr to top window
        self.old_stdout = sys.stdout
        self.old_stderr = sys.stderr

        writer = Writer(self.top, self.top_lock)
        self.writer = writer
        sys.stdout = writer
        sys.stderr = writer

        # Always start log watcher for bottom window (sync and build both use it)
        self._start_watcher()

        # Start build in background thread
        if self.build_func:
            args_tuple, kwargs_dict = self.build_args
            def run_build_and_capture():
                try:
                    self.build_result = self.build_func(*args_tuple, **kwargs_dict)
                except Exception as e:
                    self.build_result = False
            self.build_thread = threading.Thread(
                target=run_build_and_capture, daemon=True
            )
            self.build_thread.start()

        # Wait for build to complete or user presses 'q'
        try:
            if self.build_thread:
                while self.build_thread.is_alive():
                    # Drain any pending top-pane output from background writers in UI thread
                    try:
                        while True:
                            item = self.top_queue.get_nowait()
                            try:
                                if hasattr(self, "writer") and item is not None:
                                    self.writer.write(item)
                                    try:
                                        self.writer.flush()
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                    except queue.Empty:
                        pass
                    ch = stdscr.getch()
                    if ch == ord("q"):
                        break
                    time.sleep(0.05)
        except Exception:
            pass
        finally:
            # Drain remaining top_queue items before closing
            try:
                while True:
                    item = self.top_queue.get_nowait()
                    try:
                        if hasattr(self, "writer") and item is not None:
                            self.writer.write(item)
                    except Exception:
                        pass
            except queue.Empty:
                pass
            self.close()

    def set_current_log(self, log_path):
        """Notify watcher of the current log file being built and update status with basename."""
        try:
            self.current_log_queue.put_nowait(log_path)
            # Also reflect the current log in the middle status line (use stem)
            try:
                stem = Path(log_path).stem if log_path else "..."
                self.set_status(stem)
            except Exception:
                pass
        except Exception:
            pass

    def set_status(self, text):
        """Update the middle separator line with a status label (e.g. repo name).

        The separator is a single solid line across the screen with the provided
        text centered: "<kind> <repo> <phase>". Stores the last text in
        self.separator_text so background watchers can redraw it without calling
        set_status (avoids lock re-entrancy).
        """
        try:
            with self.top_lock:
                if self.stdscr is None:
                    return
                # store for watchers
                try:
                    self.separator_text = str(text)
                except Exception:
                    self.separator_text = ""
                # Build the separator: ─── text ───
                label = f" {self.separator_text} "
                total = self.w
                left_pad = max(0, (total - len(label)) // 2)
                right_pad = max(0, total - left_pad - len(label))
                line = "─" * left_pad + label + "─" * right_pad
                try:
                    self.stdscr.addstr(self.sep_row, 0, line[:total], curses.A_BOLD)
                    self.stdscr.refresh()
                except Exception:
                    pass
        except Exception:
            pass

    def _start_watcher(self):
        """Watch log files and display content in bottom pane."""

        def watcher():
            path = Path(self.log_dir) if self.log_dir else None
            last_log = None
            last_pos = 0
            current_log_file = None
            prev_log_file = None
            cur_y = 2  # current writing row in bottom pane
            bottom_buffer = []  # circular buffer of visible content lines

            while self.running:
                try:
                    # Check if a new log file has been set
                    try:
                        while True:
                            current_log_file = self.current_log_queue.get_nowait()
                    except queue.Empty:
                        pass

                    # Update header and clear bottom pane when log file changes
                    if current_log_file != prev_log_file:
                        prev_log_file = current_log_file
                        with self.bottom_lock:
                            try:
                                bot_h, bot_w = self.bottom.getmaxyx()
                                # Clear entire bottom content area (no header rows)
                                for row in range(0, bot_h):
                                    try:
                                        self.bottom.move(row, 0)
                                        self.bottom.clrtoeol()
                                    except Exception:
                                        pass
                                # Reset current write row and buffer
                                cur_y = 0
                                bottom_buffer = []
                                self.bottom.move(0, 0)
                                self.bottom.refresh()
                            except Exception:
                                pass

                    # Find current log file to read
                    if current_log_file and Path(current_log_file).exists():
                        cur = Path(current_log_file)
                    elif path and path.exists():
                        logs = sorted(
                            path.glob("*.txt"),
                            key=lambda p: p.stat().st_mtime,
                            reverse=True,
                        )
                        cur = logs[0] if logs else None
                    else:
                        cur = None

                    if not cur:
                        time.sleep(0.2)
                        continue

                    # Reset position when switching files
                    if cur != last_log:
                        last_log = cur
                        last_pos = 0

                    # Read new data from log file
                    data = ""
                    try:
                        with open(cur, "r") as f:
                            f.seek(last_pos)
                            data = f.read()
                            last_pos = f.tell()
                    except (FileNotFoundError, OSError):
                        last_pos = 0

                    # Display new data in bottom pane
                    if data:
                        with self.bottom_lock:
                            try:
                                # Clean ANSI/control codes
                                clean = CSI_RE.sub("", data)
                                clean = ESC_TWO_RE.sub("", clean)
                                clean = ESC_CHAR_RE.sub("", clean)
                                clean = CONTROL_RE_PLAIN.sub("", clean)

                                bot_h, bot_w = self.bottom.getmaxyx()
                                content_h = max(0, bot_h)

                                # Break incoming data into wrapped lines
                                new_lines = []
                                for raw_line in clean.split("\n"):
                                    try:
                                        if '\r' in raw_line:
                                            raw_line = raw_line.split('\r')[-1]

                                        if HAVE_WRAP_UTILS and HAVE_WCWIDTH:
                                            wrapped = wrap_ansi(raw_line, bot_w - 1)
                                        else:
                                            wrapped = []
                                            line = raw_line
                                            while line:
                                                wrapped.append(line[: max(bot_w - 1, 0)])
                                                line = line[max(bot_w - 1, 0):]

                                        new_lines.extend(wrapped)
                                    except Exception:
                                        pass

                                # Append to buffer and trim to fit content height
                                if new_lines:
                                    bottom_buffer.extend(new_lines)
                                    if content_h > 0 and len(bottom_buffer) > content_h:
                                        # keep most recent content_h lines
                                        bottom_buffer = bottom_buffer[-content_h:]

                                # Ensure separator and bottom header are redrawn to avoid corruption
                                try:
                                    # Prefer separator_text if set by set_status(), fallback to stem
                                    sep_text = self.separator_text if getattr(self, 'separator_text', None) else (Path(current_log_file).stem if current_log_file else "...")
                                    # Draw middle separator directly (avoid calling set_status which would re-acquire lock)
                                    try:
                                        label = f" {sep_text} "
                                        total = self.w
                                        left_pad = max(0, (total - len(label)) // 2)
                                        right_pad = max(0, total - left_pad - len(label))
                                        sep_line = "─" * left_pad + label + "─" * right_pad
                                        if self.stdscr is not None:
                                            try:
                                                self.stdscr.addstr(self.sep_row, 0, sep_line[:total], curses.A_BOLD)
                                            except Exception:
                                                pass
                                    except Exception:
                                        pass
                                    # redraw bottom header
                                    try:
                                        # No bottom header to avoid corrupting content; ensure row 0 is cleared
                                        bot_h, bot_w = self.bottom.getmaxyx()
                                        try:
                                            self.bottom.move(0, 0)
                                            self.bottom.clrtoeol()
                                        except Exception:
                                            pass
                                    except Exception:
                                        pass
                                except Exception:
                                    pass

                                # Redraw content area from buffer (no header rows)
                                for idx in range(content_h):
                                    row = idx
                                    try:
                                        if idx < len(bottom_buffer):
                                            line = bottom_buffer[idx]
                                            try:
                                                # Truncate to window width to prevent overflow
                                                try:
                                                    # prefer addnstr if available
                                                    self.bottom.addnstr(row, 0, line, bot_w - 1)
                                                except Exception:
                                                    self.bottom.addstr(row, 0, line[: max(bot_w - 1, 0)])
                                                self.bottom.clrtoeol()
                                            except Exception:
                                                pass
                                        else:
                                            try:
                                                self.bottom.move(row, 0)
                                                self.bottom.clrtoeol()
                                            except Exception:
                                                pass
                                    except Exception:
                                        pass

                                # Refresh stdscr separator then bottom window
                                try:
                                    if self.stdscr:
                                        self.stdscr.refresh()
                                except Exception:
                                    pass
                                try:
                                    self.bottom.refresh()
                                except Exception:
                                    pass
                            except Exception:
                                pass
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
