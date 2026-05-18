"""
Build logic for una - extracted to avoid indentation issues.
"""

import sys
import os
import subprocess
import json
import shutil
from pathlib import Path
import contextlib
import threading
import queue
import re

from mods.trace import (
    is_enabled,
    tools_step_start,
    tools_step_end,
    build_step_start,
    build_step_end,
)
from mods.utils import get_all_arches

# These will be set by init_build()
colors = None
load_repo_una = None
StepRunner = None
get_target_triple = None
get_arch_flags = None
propagate_skel = None
sync_kernel_config = None
is_repo_dirty = None
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
git_logs_dir = None
curses_ui = None
# Sync-related globals
sync_repo_func = None
save_repo_state_func = None
repos_config_all = None
repos_to_sync_set = None
una_base_str = None


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

        # Execute subprocess
        return subprocess.run(cmd, cwd=cwd, env=env, check=check, shell=shell, **kwargs)
    
    def _log_to_trace(self, cmd, env_display, cmd_display):
        """Log command details to trace file."""
        try:
            with open(self.trace_file, "a") as f:
                f.write("=" * 80 + "\n")
                f.write(f"Command: {cmd_display}\n")
                
                if cwd:
                    f.write(f"WorkDir: {cwd}\n")
                
                if env:
                    f.write("Environment:\n")
                    f.write(f"{env_display}\n")
                
                f.write("=" * 80 + "\n")
                f.flush()
        except Exception as e:
            colors.warn(f"Failed to write to trace file {self.trace_file}: {e}")


def strip_ansi_codes(text):
    """Remove ANSI escape sequences from text."""
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)


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
    colors_mod,
    load_repo_una_func,
    StepRunner_class,
    get_target_triple_func,
    get_arch_flags_func,
    propagate_skel_func,
    sync_kernel_config_func,
    is_repo_dirty_func,
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
    sync_repo_func_val=None,
    save_repo_state_func_val=None,
    repos_config_all_val=None,
    repos_to_sync_set_val=None,
    una_base_str_val=None,
):
    """Initialize the build module with required functions and variables."""
    global colors, load_repo_una, StepRunner, get_target_triple
    global get_arch_flags, propagate_skel, sync_kernel_config, is_repo_dirty
    global BASE_DIR, bld_base, arches, repos, repos_to_process
    global required_names, build_all, tools_install_dir, skel_dir, global_cfg, use_curses, git_logs_dir, curses_ui
    global sync_repo_func, save_repo_state_func, repos_config_all, repos_to_sync_set, una_base_str

    colors = colors_mod
    load_repo_una = load_repo_una_func
    StepRunner = StepRunner_class
    get_target_triple = get_target_triple_func
    get_arch_flags = get_arch_flags_func
    propagate_skel = propagate_skel_func
    sync_kernel_config = sync_kernel_config_func
    is_repo_dirty = is_repo_dirty_func
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
    sync_repo_func = sync_repo_func_val
    save_repo_state_func = save_repo_state_func_val
    repos_config_all = repos_config_all_val
    repos_to_sync_set = repos_to_sync_set_val
    una_base_str = una_base_str_val


def _run_sync_phase():
    """Run git sync for all repos inside the curses UI."""
    if not sync_repo_func or not repos_config_all or not repos_to_sync_set:
        return

    colors.info("\n--- Git Sync Stage ---")
    for cfg in repos_config_all:
        if cfg.get("is_virtual") or cfg["name"] not in repos_to_sync_set:
            continue
        if "repo_dir" not in cfg:
            continue

        name = cfg["name"]
        git_log_file = git_logs_dir / f"{name}_git_pre.txt"

        # Update curses UI: repo name in separator, git log in bottom pane
        if curses_ui:
            curses_ui.set_status(name)
            curses_ui.set_current_log(str(git_log_file))

        colors.info(f">>> Syncing repo: {name}")

        # Redirect FD-level stdout/stderr to log file (captures git subprocess output)
        # Python-level sys.stdout (curses Writer) is unaffected -> status stays in top pane
        original_stdout_fd = os.dup(1)
        original_stderr_fd = os.dup(2)
        try:
            with open(git_log_file, "w") as f:
                os.dup2(f.fileno(), 1)
                os.dup2(f.fileno(), 2)
                if sync_repo_func(cfg, una_base_str):
                    save_repo_state_func(cfg)
        finally:
            os.dup2(original_stdout_fd, 1)
            os.dup2(original_stderr_fd, 2)
            os.close(original_stdout_fd)
            os.close(original_stderr_fd)

    colors.info("Git sync complete.")
    # Reset separator for build phase
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
            
            original_stdout_fd = os.dup(1)
            original_stderr_fd = os.dup(2)
            old_sys_stdout = sys.stdout
            old_sys_stderr = sys.stderr
            
            try:
                log_file = open(log_file_path, "a")
                
                # If curses is active, use pipe capture to write to both curses UI and log file
                if use_curses and hasattr(old_sys_stdout, 'write'):
                    pipe_read, pipe_write = os.pipe()
                    try:
                        os.set_inheritable(pipe_read, False)
                    except Exception:
                        pass
                    try:
                        os.set_inheritable(pipe_write, False)
                    except Exception:
                        pass
                    # Redirect FD stdout/stderr to pipe
                    os.dup2(pipe_write, 1)
                    os.dup2(pipe_write, 2)
                    try:
                        os.set_inheritable(1, True)
                        os.set_inheritable(2, True)
                    except Exception:
                        pass
                    # Close original write fd; FD1/2 now reference the pipe. This avoids
                    # leaving extra write-end references that would prevent the reader seeing EOF.
                    try:
                        os.close(pipe_write)
                    except Exception:
                        pass

                    # Debug: write fd list to logfile to help diagnose stray fds
                    try:
                        try:
                            fds = sorted(os.listdir('/proc/self/fd'))
                        except Exception:
                            fds = []
                        try:
                            log_file.write(f"DEBUG_FDS_AFTER_DUP: {fds}\n")
                            log_file.flush()
                            try:
                                os.fsync(log_file.fileno())
                            except Exception:
                                pass
                        except Exception:
                            pass
                    except Exception:
                        pass
                    
                    # Create async writer to handle both log file and curses UI
                    class AsyncLogWriter:
                        def __init__(self, logfile, write_to_top=True, top_queue=None):
                            self.logfile = logfile
                            self.write_to_top = bool(write_to_top)
                            self.top_queue = top_queue
                            self.q = queue.Queue()
                            self.thread = threading.Thread(target=self._run, daemon=True)
                            self.thread.start()
                        
                        def _run(self):
                            while True:
                                try:
                                    item = self.q.get()
                                except Exception:
                                    continue
                                if item is None:
                                    break
                                text = item
                                if isinstance(text, str):
                                    text = text.replace('\r', '')
                                try:
                                    if hasattr(self.logfile, 'write'):
                                        # Strip ANSI codes for log file
                                        clean_text = strip_ansi_codes(text)
                                        self.logfile.write(clean_text)
                                        try:
                                            self.logfile.flush()
                                            try:
                                                os.fsync(self.logfile.fileno())
                                            except Exception:
                                                pass
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                                # Enqueue top text for UI thread; avoid calling curses from this background thread
                                if self.write_to_top and self.top_queue is not None:
                                    try:
                                        self.top_queue.put(text)
                                    except Exception:
                                        pass
                                try:
                                    self.q.task_done()
                                except Exception:
                                    pass
                            try:
                                if hasattr(self.logfile, 'flush'):
                                    self.logfile.flush()
                            except Exception:
                                pass
                        
                        def put(self, txt):
                            try:
                                self.q.put(txt)
                            except Exception:
                                pass
                        
                        def flush(self):
                            try:
                                self.q.join()
                            except Exception:
                                pass
                    
                    async_writer = AsyncLogWriter(log_file, write_to_top=True, top_queue=(curses_ui.top_queue if curses_ui else None))
                    
                    # Rebind sys.stdout/stderr to StdoutReplacer that queues writes
                    class StdoutReplacer:
                        def __init__(self, aw):
                            self.aw = aw
                        def write(self, txt):
                            try:
                                if isinstance(txt, str):
                                    txt = txt.replace('\r', '')
                                self.aw.put(txt)
                            except Exception:
                                pass
                            return len(txt)
                        def flush(self):
                            try:
                                self.aw.flush()
                            except Exception:
                                pass
                        def isatty(self):
                            return False
                    
                    sys.stdout = StdoutReplacer(async_writer)
                    sys.stderr = sys.stdout
                    
                    # Pipe reader thread
                    def reader_thread():
                        while True:
                            try:
                                data = os.read(pipe_read, 4096)
                                if not data:
                                    break
                                async_writer.put(data.decode('utf-8', errors='replace'))
                            except Exception:
                                break
                    
                    reader = threading.Thread(target=reader_thread, daemon=True)
                    reader.start()
                    
                    # Run tool build
                    if hasattr(module, "tools_configure"):
                        module.tools_configure(tools_install_dir, arches=get_all_arches())
                    if hasattr(module, "tools_build"):
                        module.tools_build(tools_install_dir)
                    if hasattr(module, "tools_install"):
                        module.tools_install(tools_install_dir)
                    
                    # Close pipe to signal end (may already be closed after dup2)
                    try:
                        os.close(pipe_write)
                    except Exception:
                        pass
                    reader.join(timeout=2)
                    async_writer.put(None)
                else:
                    # Non-curses mode: tee output to terminal and log file
                    pipe_read, pipe_write = os.pipe()
                    try:
                        os.set_inheritable(pipe_read, False)
                    except Exception:
                        pass
                    try:
                        os.set_inheritable(pipe_write, False)
                    except Exception:
                        pass
                    os.dup2(pipe_write, 1)
                    os.dup2(pipe_write, 2)
                    try:
                        os.set_inheritable(1, True)
                        os.set_inheritable(2, True)
                    except Exception:
                        pass
                    try:
                        os.close(pipe_write)
                    except Exception:
                        pass

                    def _tee_thread():
                        try:
                            while True:
                                data = os.read(pipe_read, 4096)
                                if not data:
                                    break
                                os.write(original_stdout_fd, data)
                                log_file.write(data.decode(errors="replace"))
                                log_file.flush()
                        except Exception:
                            pass

                    tee_thread = threading.Thread(target=_tee_thread, daemon=True)
                    tee_thread.start()
                    if hasattr(module, "tools_configure"):
                        module.tools_configure(tools_install_dir, arches=get_all_arches())
                    if hasattr(module, "tools_build"):
                        module.tools_build(tools_install_dir)
                    if hasattr(module, "tools_install"):
                        module.tools_install(tools_install_dir)
            finally:
                # Close pipe_write to signal EOF to tee thread (non-curses mode)
                if not use_curses and 'pipe_write' in dir() and pipe_write is not None:
                    try:
                        os.close(pipe_write)
                    except Exception:
                        pass
                os.dup2(original_stdout_fd, 1)
                os.dup2(original_stderr_fd, 2)
                os.close(original_stdout_fd)
                os.close(original_stderr_fd)
                sys.stdout = old_sys_stdout
                sys.stderr = old_sys_stderr
                # Wait for tee thread to finish draining remaining data
                if not use_curses and 'tee_thread' in dir() and tee_thread is not None:
                    try:
                        tee_thread.join(timeout=5)
                    except Exception:
                        pass
                try:
                    log_file.close()
                except Exception:
                    pass
        
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
                ["git", "clean", "-fdx", "-e", ".una_config"], cwd=r_path, check=True
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
                        subprocess.run(["git", "clean", "-qfdx"], cwd=r_path, check=True)
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
                subprocess.run(["git", "clean", "-qfdx"], cwd=r_path, check=True)
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
                        )
                    except subprocess.CalledProcessError:
                        pass
            cleaned_dirs.add(r_path)

    # End of build process
    return True
