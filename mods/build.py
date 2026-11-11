"""
Build logic for una.
"""

import importlib.util
import json
import os
import queue
import re
import selectors
import shutil
import subprocess
import sys
import threading
import time
import contextlib
from pathlib import Path

from mods import colors
from mods.snapshot import (
    take_snapshot,
    compare_snapshots,
    write_report,
    get_report_paths,
)
from mods.trace import (
    is_enabled,
    tools_step_start,
    tools_step_end,
    build_step_start,
    build_step_end,
    trace_file_open,
    trace_file_close,
    trace_exception,
)
from mods.utils import (
    get_all_arches,
    get_target_triple,
    get_arch_flags,
    is_repo_dirty,
    strip_ansi_codes,
)
from mods.git_ops import sync_kernel_config, sync_repo
from mods.config import save_repo_state

# These will be set by init_build()
BASE_DIR = None
bld_base = None
arches = None
repos = None
repos_to_process = None
required_names = None
build_all = None
tools_install_dir = None
skel_dir = None
global_cfg = None
use_curses = False
git_logs_dir = None
curses_ui = None
repos_config_all = None
repos_to_sync_set = None
una_base_str = None


class NonBlockingPipeCapture:
    """Captures process stdout/stderr (fds 1 and 2) using non-blocking, unbuffered pipe I/O.

    Streams output unbuffered to a log file on disk and optionally to CursesUI
    (bottom pane) or original stdout without blocking or disk-polling delays.
    """
    def __init__(self, log_path, curses_ui=None):
        self.log_path = log_path
        self.curses_ui = curses_ui
        self.pipe_read = None
        self.pipe_write = None
        self.orig_stdout = None
        self.orig_stderr = None
        self.log_fd = None
        self.reader_thread = None
        self.stop_event = threading.Event()
        self.output_buffer = []

    def __enter__(self):
        if self.log_path:
            os.makedirs(os.path.dirname(os.path.abspath(str(self.log_path))), exist_ok=True)
            self.log_fd = os.open(
                str(self.log_path),
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NONBLOCK,
                0o644
            )
        self.pipe_read, self.pipe_write = os.pipe()
        os.set_blocking(self.pipe_read, False)
        os.set_inheritable(self.pipe_read, False)
        os.set_inheritable(self.pipe_write, True)
        self.orig_stdout = os.dup(1)
        self.orig_stderr = os.dup(2)
        os.dup2(self.pipe_write, 1)
        os.dup2(self.pipe_write, 2)
        os.close(self.pipe_write)
        self.pipe_write = None

        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()
        return self

    def _drain(self):
        while True:
            try:
                chunk = os.read(self.pipe_read, 32768)
            except (BlockingIOError, InterruptedError):
                return True
            except OSError:
                return False
            if not chunk:
                return False
            if self.log_fd is not None:
                try:
                    os.write(self.log_fd, chunk)
                except OSError:
                    pass
            self.output_buffer.append(chunk)
            if self.curses_ui:
                try:
                    if hasattr(self.curses_ui, 'write_bottom'):
                        self.curses_ui.write_bottom(chunk)
                    elif hasattr(self.curses_ui, 'top_queue'):
                        self.curses_ui.top_queue.put(('bottom', chunk.decode('utf-8', errors='replace')))
                except Exception:
                    pass
            elif self.orig_stdout is not None:
                try:
                    os.write(self.orig_stdout, chunk)
                except OSError:
                    pass

    def _reader_loop(self):
        sel = selectors.DefaultSelector()
        sel.register(self.pipe_read, selectors.EVENT_READ)
        try:
            while not self.stop_event.is_set():
                events = sel.select(timeout=0.05)
                if events:
                    if not self._drain():
                        return
        finally:
            sel.close()
            deadline = time.monotonic() + 1.0
            while self._drain() and time.monotonic() < deadline:
                time.sleep(0.005)

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            os.dup2(self.orig_stdout, 1)
            os.dup2(self.orig_stderr, 2)
        except OSError:
            pass
        if self.orig_stdout is not None:
            try:
                os.close(self.orig_stdout)
            except OSError:
                pass
            self.orig_stdout = None
        if self.orig_stderr is not None:
            try:
                os.close(self.orig_stderr)
            except OSError:
                pass
            self.orig_stderr = None

        self.stop_event.set()
        if self.reader_thread is not None:
            self.reader_thread.join(timeout=3.0)
            self.reader_thread = None

        if self.pipe_read is not None:
            try:
                os.close(self.pipe_read)
            except OSError:
                pass
            self.pipe_read = None

        if self.log_fd is not None:
            try:
                os.close(self.log_fd)
            except OSError:
                pass
            self.log_fd = None


class AsyncLogWriter:
    """Deprecated: retained for compatibility."""
    def __init__(self, logfile=None, write_to_top=True, top_queue=None):
        pass
    def put(self, txt):
        pass
    def flush(self):
        pass
    def stop(self):
        pass


class StdoutReplacer:
    """Deprecated: retained for compatibility."""
    def __init__(self, aw=None):
        self.aw = aw
    def write(self, txt):
        return len(txt) if txt else 0
    def flush(self):
        pass
    def isatty(self):
        return False


def get_build_env(staging_dir=None):
    """Get environment for build subprocesses with tools/bin in PATH.
    
    Args:
        staging_dir: Optional Path to staging directory for PKG_CONFIG_PATH.
                    Used by packages that depend on libs in staging.
    
    Returns:
        dict: Environment with tools/bin added to PATH and optional PKG_CONFIG_PATH.
    """
    env = os.environ.copy()
    tools_bin = Path.cwd() / "bld" / "tools" / "bin"
    env["PATH"] = f"{tools_bin}:{env.get('PATH', '')}"
    env["PYTHONUNBUFFERED"] = "1"
    
    if staging_dir:
        env["PKG_CONFIG_PATH"] = f"{staging_dir}/usr/lib/pkgconfig"
    
    return env


class SubprocessRunner:
    """Wrapper for subprocess.run with trace logging and execution info output."""
    
    def __init__(self, trace_file=None):
        """Initialize runner with optional trace file.
        
        Args:
            trace_file: Optional Path to trace file for logging commands.
        """
        self.trace_file = trace_file
    
    def run(self, cmd, cwd=None, env=None, check=True, shell=False, **kwargs):
        """Execute command with logging and trace support.
        
        Args:
            cmd: Command and arguments as list or string (if shell=True).
            cwd: Working directory for subprocess.
            env: Environment variables dict.
            check: Raise CalledProcessError if returncode != 0.
            shell: Execute command through shell.
            **kwargs: Additional arguments passed to subprocess.run.
        
        Returns:
            CompletedProcess: Result from subprocess.run.
        """
        from mods import colors
        import shlex
        
        # Format command for display
        if isinstance(cmd, list):
            cmd_display = " ".join(shlex.quote(str(c)) for c in cmd)
        else:
            cmd_display = cmd

        # Format command for env
        if env:
            env_display = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in env.items())
        else:
            env_display = ""
        # Log to trace file if provided and exists
        if self.trace_file:
            self._log_to_trace(cwd, env_display, cmd_display)
        
        # Display execution info
        if cwd:
            colors.info(f"Executing in: {cwd}")
        colors.info(f"{env_display} {cmd_display}")

        # Execute subprocess with stdin closed and unbuffered I/O
        if 'stdin' not in kwargs:
            kwargs['stdin'] = subprocess.DEVNULL
        if env is not None:
            run_env = env.copy()
        else:
            run_env = os.environ.copy()
        run_env.setdefault("PYTHONUNBUFFERED", "1")
        return subprocess.run(cmd, cwd=cwd, env=run_env, check=check, shell=shell, **kwargs)
    
    def _log_to_trace(self, cwd, env_display, cmd_display):
        """Log command details to trace file."""
        try:
            with open(self.trace_file, "a") as f:
                f.write("=" * 80 + "\n")
                f.write(f"Command: {cmd_display}\n")
                
                if cwd:
                    f.write(f"WorkDir: {cwd}\n")
                
                if env_display:
                    f.write("Environment:\n")
                    f.write(f"{env_display}\n")
                
                f.write("=" * 80 + "\n")
                f.flush()
        except Exception as e:
            colors.warn(f"Failed to write to trace file {self.trace_file}: {e}")


@contextlib.contextmanager
def redirect_git_output(log_file_path):
    """Context manager to redirect stdout/stderr to a git log file."""
    original_stdout_fd = os.dup(1)
    original_stderr_fd = os.dup(2)
    try:
        log_file = open(log_file_path, "a")
        os.dup2(log_file.fileno(), 1)
        os.dup2(log_file.fileno(), 2)
        yield
    finally:
        log_file.close()
        os.dup2(original_stdout_fd, 1)
        os.dup2(original_stderr_fd, 2)
        os.close(original_stdout_fd)
        os.close(original_stderr_fd)


def init_build(
    BASE_DIR_val,
    bld_base_val,
    arches_val,
    repos_val,
    repos_to_process_val,
    required_names_val,
    build_all_val,
    tools_install_dir_val,
    skel_dir_val,
    global_cfg_val,
    use_curses_val=False,
    git_logs_dir_val=None,
    curses_ui_val=None,
    repos_config_all_val=None,
    repos_to_sync_set_val=None,
    una_base_str_val=None,
):
    """Initialize the build module with required variables."""
    global BASE_DIR, bld_base, arches, repos, repos_to_process
    global required_names, build_all, tools_install_dir, skel_dir, global_cfg, use_curses, git_logs_dir, curses_ui
    global repos_config_all, repos_to_sync_set, una_base_str

    BASE_DIR = BASE_DIR_val
    bld_base = bld_base_val
    arches = arches_val
    repos = repos_val
    repos_to_process = repos_to_process_val
    required_names = required_names_val
    build_all = build_all_val
    tools_install_dir = tools_install_dir_val
    skel_dir = skel_dir_val
    global_cfg = global_cfg_val
    use_curses = use_curses_val
    git_logs_dir = git_logs_dir_val
    curses_ui = curses_ui_val
    repos_config_all = repos_config_all_val
    repos_to_sync_set = repos_to_sync_set_val
    una_base_str = una_base_str_val


def load_repo_una(repo_dir, una_file_name="una.py"):
    """Dynamically load the specified una file from the repo directory."""
    una_file = Path(repo_dir) / una_file_name
    if not una_file.exists():
        colors.error(
            f"Error: {una_file} not found. Build script is missing for this component."
        )
        sys.exit(1)

    unique_id = (
        f"{Path(repo_dir).name}_{una_file_name.replace('/', '_').replace('.', '_')}"
    )
    module_name = f"repo_una_{unique_id}"

    spec = importlib.util.spec_from_file_location(module_name, una_file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class StepRunner:
    def __init__(self, arch, staging_dir, target_dir, bld_base, use_pipe_capture=False, curses_ui=None):
        self.arch = arch
        self.staging_dir = staging_dir
        self.target_dir = target_dir
        self.bld_base = bld_base
        self.use_pipe_capture = use_pipe_capture
        self.curses_ui = curses_ui
        self.component_snapshots = {}
        self.cleaned_components = set()
        self.build_logs_dir = self.bld_base / "build_logs"
        self.build_logs_dir.mkdir(parents=True, exist_ok=True)

    def run_step(self, cfg, step_name, step_func, **kwargs):
        name = cfg["name"]
        colors.info(f"[{self.arch}] Running {name}::{step_name}...")
        if is_enabled():
            build_step_start(self.arch, name, step_name)

        if name not in self.cleaned_components:
            report_file = self.bld_base / "report" / f"{name}.txt"
            if report_file.exists():
                colors.info(
                    f"[{self.arch}] Cleaning up previous build outputs for {name}..."
                )
                paths = get_report_paths(report_file)
                for p in paths:
                    try:
                        if p.startswith("staging/"):
                            (self.staging_dir / p[8:]).unlink(missing_ok=True)
                        elif p.startswith("target/"):
                            (self.target_dir / p[7:]).unlink(missing_ok=True)
                    except Exception as e:
                        colors.warn(f"[{self.arch}] Warning: Failed to remove {p}: {e}")

            self.component_snapshots[name] = {
                "staging": take_snapshot(self.staging_dir),
                "target": take_snapshot(self.target_dir),
            }
            self.cleaned_components.add(name)

        log_file_path = self.build_logs_dir / f"{name}.txt"
        colors.info(f"[{self.arch}] Build log: {log_file_path}")

        if self.curses_ui:
            try:
                kind = 'tools' if cfg.get('type') == 'tools' else (getattr(self, 'arch', '') or '')
                phase = None
                if isinstance(step_name, str):
                    lname = step_name.lower()
                    if 'configure' in lname:
                        phase = 'configure'
                    elif 'install' in lname:
                        phase = 'install'
                    elif 'build' in lname:
                        phase = 'build'
                if not phase:
                    phase = str(step_name)
                parts = [p for p in (kind, name, phase) if p]
                label = " ".join(parts)
                try:
                    self.curses_ui.set_status(label)
                except Exception:
                    pass
                try:
                    self.curses_ui.set_current_log(str(log_file_path))
                except Exception:
                    pass
            except Exception:
                try:
                    self.curses_ui.set_current_log(str(log_file_path))
                except Exception:
                    pass

        trace_file_open(str(log_file_path), "a")
        try:
            with NonBlockingPipeCapture(log_file_path, curses_ui=self.curses_ui if self.use_pipe_capture else None):
                step_func(self.staging_dir, self.target_dir, **kwargs)
        except Exception as e:
            trace_exception(f"step_func({name}::{step_name})", e)
            raise
        finally:
            trace_file_close(str(log_file_path))

        pre = self.component_snapshots[name]
        post_staging = take_snapshot(self.staging_dir)
        post_target = take_snapshot(self.target_dir)

        added_s, mod_s, del_s = compare_snapshots(pre["staging"], post_staging)
        added_t, mod_t, del_t = compare_snapshots(pre["target"], post_target)

        if mod_s or del_s:
            colors.error(
                f"[{self.arch}] ERROR: {name} modified or deleted files in staging!"
            )
        if mod_t or del_t:
            colors.error(
                f"[{self.arch}] ERROR: {name} modified or deleted files in target!"
            )

        combined_added = {f"staging/{k}": v for k, v in added_s.items()}
        combined_added.update({f"target/{k}": v for k, v in added_t.items()})

        combined_mod = {f"staging/{k}": v for k, v in mod_s.items()}
        combined_mod.update({f"target/{k}": v for k, v in mod_t.items()})

        combined_del = {f"staging/{k}": v for k, v in del_s.items()}
        combined_del.update({f"target/{k}": v for k, v in del_t.items()})

        report_file = self.bld_base / "build_product" / f"{name}.txt"
        write_report(combined_added, combined_mod, combined_del, report_file)

        if is_enabled():
            build_step_end(self.arch, name, step_name)


def propagate_skel(staging_dir, target_dir):
    """Skel propagation using original file-by-file method + snapshot verification"""
    global skel_dir

    colors.info("Propagating skeleton (original method)...")
    for dest in [staging_dir, target_dir]:
        for item in os.listdir(skel_dir):
            s_item = skel_dir / item
            d_item = dest / item

            if (
                d_item.exists()
                and s_item.is_symlink()
                and d_item.is_dir()
                and not d_item.is_symlink()
            ):
                colors.warn(
                    f"Removing conflicting directory {d_item} to preserve skel symlink."
                )
                shutil.rmtree(d_item)

        subprocess.run(
            ["cp", "-a", "--remove-destination", f"{skel_dir}/.", str(dest)], check=True,
            stdin=subprocess.DEVNULL,
        )


def _run_sync_phase():
    """Run git sync for all repos inside the curses UI."""
    if not repos_config_all or not repos_to_sync_set:
        return

    colors.info("\n--- Git Sync Stage ---")
    for cfg in repos_config_all:
        if cfg.get("is_virtual") or cfg["name"] not in repos_to_sync_set:
            continue
        if "repo_dir" not in cfg:
            continue

        name = cfg["name"]
        git_log_file = git_logs_dir / f"{name}_git_pre.txt"

        if curses_ui:
            curses_ui.set_status(name)
            curses_ui.set_current_log(str(git_log_file))

        colors.info(f">>> Syncing repo: {name}")
        from mods.git_ops import sync_repo
        from mods.config import save_repo_state

        with NonBlockingPipeCapture(git_log_file, curses_ui=curses_ui if use_curses else None):
            if sync_repo(cfg, una_base_str, BASE_DIR):
                save_repo_state(cfg)

        if cfg.get("rebased_new_tag"):
            colors.info(
                f"Updated '{name}' to tag '{cfg['rebased_new_tag']}' (rebased and pushed)."
            )
        elif cfg.get("newer_tag"):
            colors.warn(
                f"Notice: A newer version tag '{cfg['newer_tag']}' is available for '{name}' (configured: '{cfg.get('tag')}')"
            )

    colors.info("Git sync complete.")
    if curses_ui:
        curses_ui.set_status("Build")


def run_build(args):
    """Main build function."""
    # Run sync phase first (inside curses UI when active)
    _run_sync_phase()

    colors.info("Starting build process.")
    tools_state_file = BASE_DIR / "bld" / "tools" / "tools_state"

    def get_repo_commit(repo_path):
        if not (repo_path / ".git").exists():
            return None
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_path, capture_output=True, text=True
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def load_tools_state():
        if not tools_state_file.exists():
            return {}
        try:
            with open(tools_state_file, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_tools_state(state):
        tools_state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(tools_state_file, "w") as f:
            json.dump(state, f, indent=2)

    def check_tools_changed(tools_repos):
        current_state = {
            r["name"]: get_repo_commit(Path(r["repo_dir"])) for r in tools_repos
        }
        saved_state = load_tools_state()

        for name, commit in current_state.items():
            if commit is None:
                continue
            if name not in saved_state or saved_state[name] != commit:
                return True
        return False

    all_tools_repos = [r for r in repos if r.get("type") == "tools"]
    # Sort tools by dependency order
    from mods.deps import get_build_order
    try:
        ordered_names, _ = get_build_order(all_tools_repos)
        name_to_repo = {r["name"]: r for r in all_tools_repos}
        all_tools_repos = [name_to_repo[name] for name in ordered_names if name in name_to_repo]
    except Exception as e:
        colors.warn(f"Failed to sort tools by dependency: {e}")

    # Also check if any component explicitly requested tools (like build-tools)
    has_tool_component = "build-tools" in required_names

    tools_to_build = []
    if build_all:
        # Check if tools need rebuilding
        if not tools_state_file.exists():
            colors.info("Tools not built, building...")
            tools_to_build = all_tools_repos
        elif check_tools_changed(all_tools_repos):
            colors.info("Tools source changed, will rebuild...")
            tools_to_build = all_tools_repos
        else:
            colors.info("Tools already built and up to date, skipping...")
    elif repos_to_process:
        target_requires_tools = any(r.get("type") != "tools" for r in repos_to_process)
        requested_any_tool = any(r["name"] in required_names for r in all_tools_repos)
        if target_requires_tools or requested_any_tool or has_tool_component:
            if not tools_state_file.exists():
                colors.info("Tools not built, building...")
                tools_to_build = all_tools_repos
            elif check_tools_changed(all_tools_repos):
                colors.info("Tools source changed, will rebuild...")
                tools_to_build = all_tools_repos
            else:
                colors.info("Tools already built and up to date, skipping...")
    else:
        # No specific target - this is a default --build without args
        if not tools_state_file.exists():
            colors.info("Tools not built, building...")
            tools_to_build = all_tools_repos
        elif check_tools_changed(all_tools_repos):
            colors.info("Tools source changed, will rebuild...")
            tools_to_build = all_tools_repos
        else:
            colors.info("Tools already built and up to date, skipping...")

    if tools_to_build:
        colors.info("\n--- Tools Stage ---")
        # Create build_logs directory for tools at top level
        tools_build_logs_dir = BASE_DIR / "bld" / "tools" / "build_logs"
        tools_build_logs_dir.mkdir(parents=True, exist_ok=True)
        
        if is_enabled():
            tools_step_start("tools_configure")
        for r in tools_to_build:
            colors.info(f"Building tool: {r['name']}")
            module = load_repo_una(r["repo_dir"], r.get("una_file", "una.py"))
            
            # Redirect tool build output to log file
            log_file_path = tools_build_logs_dir / f"{r['name']}.txt"
            colors.info(f"Tool log: {log_file_path}")

            # Notify curses UI of current log file
            if curses_ui and hasattr(curses_ui, 'set_current_log'):
                curses_ui.set_current_log(str(log_file_path))
            
            with NonBlockingPipeCapture(log_file_path, curses_ui=curses_ui if use_curses else None):
                if hasattr(module, "tools_configure"):
                    module.tools_configure(tools_install_dir, arches=get_all_arches())
                if hasattr(module, "tools_build"):
                    module.tools_build(tools_install_dir)
                if hasattr(module, "tools_install"):
                    module.tools_install(tools_install_dir)
        
        if is_enabled():
            tools_step_end("tools_configure")
        new_state = {
            r["name"]: get_repo_commit(Path(r["repo_dir"])) for r in tools_to_build
        }
        save_tools_state(new_state)

    target_configs_to_build = [
        r
        for r in repos_to_process
        if r.get("type") != "tools"
        and not r.get("is_virtual")
        and r.get("type") != "virtual"
        and "repo_dir" in r
    ]
    for arch in arches:
        colors.info(f"\n====== Target Stage: {arch} ======")
        staging_dir = bld_base / "staging"
        target_dir = bld_base / "target"
        runner = StepRunner(arch, staging_dir, target_dir, bld_base, use_curses, curses_ui)

        # Ensure build directories exist and skel is propagated
        staging_dir.mkdir(parents=True, exist_ok=True)
        target_dir.mkdir(parents=True, exist_ok=True)
        if skel_dir.exists():
            colors.info(f"[{arch}] Phase -1: Skeleton Propagation (verified)")
            skel_runner = StepRunner(arch, staging_dir, target_dir, bld_base, use_curses, curses_ui)
            skel_runner.run_step(
                cfg={"name": "skel"}, step_name="propagate", step_func=propagate_skel
            )
        else:
            colors.warn(f"[{arch}] No skel directory found - empty staging/target")

        # Clean ALL target repos before build to avoid stale configs/artifacts between arches
        cleaned_dirs = set()
        tools_dirs = {
            Path(r["repo_dir"]).absolute()
            for r in repos
            if r.get("type") == "tools" and "repo_dir" in r
        }
        all_target_repos = [
            r for r in repos if r.get("type") != "tools" and "repo_dir" in r
        ]

        for r in all_target_repos:
            r_path = Path(r["repo_dir"]).absolute()
            if r_path in cleaned_dirs:
                continue
            if r_path in tools_dirs:
                colors.info(
                    f"[{arch}] Skipping git clean for {r['name']} (shared with tools components)"
                )
                continue
            if not r_path.exists():
                continue
            colors.info(f"[{arch}] Cleaning {r['name']} ({r_path})...")
            if is_repo_dirty(r_path):
                colors.error(
                    f"[{arch}] ERROR: Repository {r['name']} is dirty. Please commit or stash changes before building."
                )
                sys.exit(1)
            colors.info(f">>> git clean -fdx -e .una_config  (in {r_path})")
            subprocess.run(
                ["git", "clean", "-fdx", "-e", ".una_config"], cwd=r_path, check=True,
                stdin=subprocess.DEVNULL,
            )
            # Ensure submodules are also cleaned
            if (r_path / ".gitmodules").exists():
                try:
                    colors.info(f">>> git submodule foreach --recursive git clean -fdx -e .una_config  (in {r_path})")
                    subprocess.run(
                        [
                            "git",
                            "submodule",
                            "foreach",
                            "--recursive",
                            "git",
                            "clean",
                            "-fdx",
                            "-e",
                            ".una_config",
                        ],
                        cwd=r_path,
                        check=True,
                        stdin=subprocess.DEVNULL,
                    )
                except subprocess.CalledProcessError:
                    pass
            cleaned_dirs.add(r_path)

        target_triple = get_target_triple(arch)
        march = get_arch_flags(arch)
        extra_flags = "-fcf-protection=none\n" if arch in ("aarch64", "riscv64") else ""
        ld_musl = f"/usr/lib/ld-musl-{arch}.so.1"
        if arch == "x32":
            ld_musl = "/usr/lib/ld-musl-x32.so.1"
        elif arch == "x86_64":
            ld_musl = "/usr/lib/ld-musl-x86_64.so.1"

        musl_cfg = bld_base / "musl.cfg"
        musl_cxx_cfg = bld_base / "musl_cxx.cfg"
        musl_static_cfg = bld_base / "musl_static.cfg"

        if (
            not musl_cfg.exists()
            or not musl_cxx_cfg.exists()
            or not musl_static_cfg.exists()
        ):
            colors.info(f"[{arch}] Generating compiler configurations...")
            lld_path = tools_install_dir / "bin" / "ld.lld"
            lib_p = staging_dir / "usr" / "lib"

            # Common flags
            common_flags = (
                f"--target={target_triple}\n--sysroot={staging_dir}\n-fPIE\n{march}\n{extra_flags}"
            )

            builtins_link = f"-L{staging_dir}/usr/lib/linux\n-lclang_rt.builtins-{arch}-bmf\n"

            # Pure C Config
            musl_cfg.write_text(
                f"{common_flags}-isystem {staging_dir}/usr/include\n-fuse-ld={lld_path}\n-nostdlib\n-L{staging_dir}/usr/lib\n-lc\n{builtins_link}-Wl,-dynamic-linker,{ld_musl}\n"
            )

            # C++ Config
            musl_cxx_cfg.write_text(
                f"{common_flags}-isystem {staging_dir}/usr/include/c++/v1\n-isystem {staging_dir}/usr/include\n--ld-path={lld_path}\n-nostdlib\n{lib_p}/Scrt1.o\n{lib_p}/crti.o\n-L{lib_p}\n-lc++\n-lc++abi\n-lunwind\n-lc\n{builtins_link}{lib_p}/crtn.o\n-Wl,-dynamic-linker,{ld_musl}\n"
            )

            # Static Config
            musl_static_cfg.write_text(
                f"{common_flags}-isystem {staging_dir}/usr/include\n-fuse-ld={lld_path}\n-nostdlib\n{lib_p}/Scrt1.o\n{lib_p}/crti.o\n-L{lib_p}\n-lc\n{builtins_link}{lib_p}/crtn.o\n-Wl,-dynamic-linker,{ld_musl}\n"
            )

        cpu_flags = global_cfg.get("cpu_flags", "")
        os.environ["CFLAGS"] = (
            f"--config={musl_cfg} -pipe -D_FILE_OFFSET_BITS=64 {cpu_flags}"
        )
        os.environ["CXXFLAGS"] = (
            f"--config={musl_cxx_cfg} -pipe -D_FILE_OFFSET_BITS=64 {cpu_flags}"
        )
        os.environ["CFLAGS_STATIC"] = (
            f"--config={musl_static_cfg} -pipe -D_FILE_OFFSET_BITS=64 {cpu_flags}"
        )
        os.environ["CPPFLAGS"] = f"-D_FILE_OFFSET_BITS=64 {cpu_flags}"

        colors.info(f"[{arch}] Building Target Components in Dependency Order")
        for r in target_configs_to_build:
            if r.get("is_virtual") or r.get("type") == "virtual":
                colors.info(f"[{arch}] Skipping virtual component '{r['name']}'")
                continue

            colors.info(f"[{arch}] Processing component: {r['name']}")

            if r["name"] == "linux-image":
                skel_etc = None
                if "etc_dir" in global_cfg:
                    skel_etc = BASE_DIR / global_cfg["etc_dir"]
                else:
                    skel_etc = skel_dir / "etc"

                if skel_etc.exists():
                    colors.info(
                        f"[{arch}] Finalizing: Replacing /etc with {skel_etc} before kernel build..."
                    )
                    shutil.rmtree(staging_dir / "etc", ignore_errors=True)
                    shutil.rmtree(target_dir / "etc", ignore_errors=True)
                    shutil.copytree(skel_etc, staging_dir / "etc", symlinks=True)
                    shutil.copytree(skel_etc, target_dir / "etc", symlinks=True)
                elif "etc_dir" in global_cfg:
                    colors.error(
                        f"[{arch}] Error: Skel etc override path {skel_etc} does not exist."
                    )
                    sys.exit(1)

            colors.info(f"[{arch}] DEBUG: Building repo '{r['name']}'")
            colors.info(f"[{arch}] DEBUG: Repo path: {r['repo_dir']}")
            
            # Explicitly remove build directories to ensure a fresh cmake/build
            r_path = Path(r["repo_dir"]).absolute()
            shutil.rmtree(r_path / f"build-{arch}", ignore_errors=True)
            shutil.rmtree(r_path / f"build-{r['name']}-{arch}", ignore_errors=True)

            una_file = r.get("una_file", "una.py")
            colors.info(f"[{arch}] DEBUG: Loading module '{una_file}'")
            module = load_repo_una(r["repo_dir"], una_file)
            colors.info(f"[{arch}] DEBUG: Loaded module name: {module.__name__}")
            kwargs = {"arch": arch}

            if r["name"] in ["linux-headers", "linux-image"]:
                kconfig = None
                if "kconfig" in global_cfg:
                    kconfig = BASE_DIR / global_cfg["kconfig"].replace("<arch>", arch)
                if not kconfig:
                    kconfig = BASE_DIR / "confs" / f"kernel.{arch}.config"
                kwargs["kconfig"] = Path(kconfig).absolute()

            if hasattr(module, "target_configure"):
                if is_enabled():
                    build_step_start(arch, r["name"], "target_configure")
                runner.run_step(
                    r, "target_configure", module.target_configure, **kwargs
                )
                if is_enabled():
                    build_step_end(arch, r["name"], "target_configure")
            if hasattr(module, "target_headers_install"):
                if is_enabled():
                    build_step_start(arch, r["name"], "target_headers_install")
                runner.run_step(
                    r, "target_headers_install", module.target_headers_install, **kwargs
                )
                if is_enabled():
                    build_step_end(arch, r["name"], "target_headers_install")
            if hasattr(module, "target_build"):
                if is_enabled():
                    build_step_start(arch, r["name"], "target_build")
                runner.run_step(r, "target_build", module.target_build, **kwargs)
                if is_enabled():
                    build_step_end(arch, r["name"], "target_build")
            if hasattr(module, "target_install"):
                if is_enabled():
                    build_step_start(arch, r["name"], "target_install")
                runner.run_step(r, "target_install", module.target_install, **kwargs)
                if is_enabled():
                    build_step_end(arch, r["name"], "target_install")

            if r["name"] == "linux-image" and "kernel_image" in r:
                image_map = r["kernel_image"]
                if arch in image_map:
                    rel_path = image_map[arch]
                    src_img = Path(r["repo_dir"]) / rel_path

                    kernel_name = global_cfg.get("kernel_name", "kernel")
                    dest_img = bld_base / kernel_name

                    if src_img.exists():
                        print(f"[{arch}] Copying kernel image to {dest_img}")
                        shutil.copy(src_img, dest_img)
                    else:
                        print(f"[{arch}] Warning: Kernel image not found at {src_img}")

                    # Copy initfilelist with .txt extension
                    src_initfilelist = Path(r["repo_dir"]) / "initfilelist"
                    dest_initfilelist = bld_base / f"{kernel_name}.list"
                    if src_initfilelist.exists():
                        print(f"[{arch}] Copying initfilelist to {dest_initfilelist}")
                        shutil.copy(src_initfilelist, dest_initfilelist)
                    else:
                        print(f"[{arch}] Warning: initfilelist not found at {src_initfilelist}")

                    # Sync back updated config to source
                    src_config = Path(r["repo_dir"]) / ".config"
                    if src_config.exists():
                        kconfig_path = kwargs.get("kconfig")
                        print(
                            f"[{arch}] Syncing back sanitized updated kernel config to {kconfig_path}"
                        )
                        sync_kernel_config(src_config, kconfig_path)
                else:
                    print(
                        f"[{arch}] Warning: No kernel image path defined for this architecture"
                    )

    # Post-build cleanup for workspace repositories
    print("\n--- Post-build Workspace Cleanup ---")
    cleaned_dirs = set()
    for r in repos:
        # Skip virtual components
        if r.get("is_virtual") or r.get("type") == "virtual" or "repo_dir" not in r:
            continue
        r_path = Path(r["repo_dir"]).absolute()
        if r_path in cleaned_dirs:
            continue
        if r_path.exists() and (r_path / ".git").exists():
            print(f"Cleaning {r['name']} ({r['repo_dir']})...")
            if is_repo_dirty(r_path):
                print(
                    f"ERROR: Repository {r['name']} is dirty. Skipping post-build cleanup for this repo."
                )
                continue
            # Redirect git output to component_git_post.txt
            if git_logs_dir:
                git_log_file = git_logs_dir / f"{r['name']}_git_post.txt"
                original_stdout_fd = os.dup(1)
                original_stderr_fd = os.dup(2)
                try:
                    with open(git_log_file, "w") as f:
                        os.dup2(f.fileno(), 1)
                        os.dup2(f.fileno(), 2)
                        print(f">>> git clean -qfdx  (in {r_path})")
                        subprocess.run(["git", "clean", "-qfdx"], cwd=r_path, check=True, stdin=subprocess.DEVNULL)
                        if (r_path / ".gitmodules").exists():
                            try:
                                print(f">>> git submodule foreach --recursive git clean -qfdx  (in {r_path})")
                                subprocess.run(
                                    [
                                        "git",
                                        "submodule",
                                        "foreach",
                                        "--recursive",
                                        "git",
                                        "clean",
                                        "-qfdx",
                                    ],
                                    cwd=r_path,
                                    check=True,
                                    stdin=subprocess.DEVNULL,
                                )
                            except subprocess.CalledProcessError:
                                pass
                finally:
                    os.dup2(original_stdout_fd, 1)
                    os.dup2(original_stderr_fd, 2)
                    os.close(original_stdout_fd)
                    os.close(original_stderr_fd)
            else:
                print(f">>> git clean -qfdx  (in {r_path})")
                subprocess.run(["git", "clean", "-qfdx"], cwd=r_path, check=True, stdin=subprocess.DEVNULL)
                if (r_path / ".gitmodules").exists():
                    try:
                        print(f">>> git submodule foreach --recursive git clean -qfdx  (in {r_path})")
                        subprocess.run(
                            [
                                "git",
                                "submodule",
                                "foreach",
                                "--recursive",
                                "git",
                                "clean",
                                "-qfdx",
                            ],
                            cwd=r_path,
                            check=True,
                            stdin=subprocess.DEVNULL,
                        )
                    except subprocess.CalledProcessError:
                        pass
            cleaned_dirs.add(r_path)

    # End of build process
    return True
