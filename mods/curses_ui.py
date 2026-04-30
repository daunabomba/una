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
    ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')
    
    def __init__(self, win, lock, width):
        self.win = win
        self.lock = lock
        self.width = width
        
    def write(self, text):
        with self.lock:
            # Strip ANSI escape codes for curses display
            text = self.ANSI_RE.sub('', text)
            for line in text.rstrip().split('\n'):
                if line:
                    line = line[:self.width - 2]
                try:
                    self.win.addstr(line + '\n')
                    self.win.refresh()
                except Exception:
                    pass
        return len(text)
        
    def flush(self):
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
        # args is a tuple of positional arguments to pass to build_func
        self.build_args = (args, kwargs)
        curses.wrapper(self._main)
        
    def _main(self, stdscr):
        self.stdscr = stdscr
        curses.noecho()
        curses.cbreak()
        stdscr.keypad(True)
        curses.start_color()
        curses.init_pair(1, curses.COLOR_GREEN, curses.COLOR_BLACK)
        curses.init_pair(2, curses.COLOR_CYAN, curses.COLOR_BLACK)
        
        self.h, self.w = stdscr.getmaxyx()
        top_h = self.h // 2
        bot_h = self.h - top_h - 1  # -1 for stdscr
        
        self.top = curses.newwin(top_h, self.w, 0, 0)
        self.bottom = curses.newwin(bot_h, self.w, top_h, 0)
        
        self.top.scrollok(True)
        self.bottom.scrollok(True)
        
        # Headers
        self.top.addstr(0, 0, "=== Una Output ===", curses.A_BOLD | curses.color_pair(1))
        self.top.hline(1, 0, curses.ACS_HLINE, self.w)
        
        self.bottom.addstr(0, 0, "=== Build Logs ===", curses.A_BOLD | curses.color_pair(2))
        self.bottom.hline(1, 0, curses.ACS_HLINE, self.w)
        
        self.top.refresh()
        self.bottom.refresh()
        
        self.running = True
        
        # Redirect stdout/stderr
        self.old_stdout = sys.stdout
        self.old_stderr = sys.stderr
        
        sys.stdout = Writer(self.top, self.lock, self.w)
        sys.stderr = Writer(self.top, self.lock, self.w)
        
        # Start log watcher
        if self.log_dir:
            self._start_watcher()
            
        # Start build in background thread
        if self.build_func:
            # build_func is a closure, call it directly
            self.build_thread = threading.Thread(
                target=self.build_func
            )
            self.build_thread.daemon = True
            self.build_thread.start()
        
        # Wait for build to complete or user presses 'q'
        try:
            if self.build_thread:
                while self.build_thread.is_alive():
                    ch = stdscr.getch()
                    if ch == ord('q'):
                        break
                    time.sleep(0.1)
        except Exception as e:
            pass
        finally:
            try:
                self.close()
            except:
                pass
            
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
                        
                    logs = sorted(path.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
                    
                    if logs:
                        cur = logs[0]
                        if cur != last_log:
                            last_log = cur
                            last_pos = 0
                            with self.lock:
                                self.bottom.clear()
                                self.bottom.addstr(0, 0, "=== {} ===".format(cur.name), curses.A_BOLD | curses.color_pair(2))
                                self.bottom.hline(1, 0, curses.ACS_HLINE, self.w)
                                self.bottom.refresh()
                                
                        if cur.exists():
                            with open(cur, 'r') as f:
                                f.seek(last_pos)
                                data = f.read()
                                if data:
                                    with self.lock:
                                        for line in data.split('\n'):
                                            if line:
                                                line = line[:self.w - 2]
                                            self.bottom.addstr(line + '\n')
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
        
        # Only call endwin if curses was properly initialized
        try:
            curses.endwin()
        except:
            pass
            
        try:
            curses.nocbreak()
            self.stdscr.keypad(False)
            curses.echo()
            curses.endwin()
        except Exception:
            pass
