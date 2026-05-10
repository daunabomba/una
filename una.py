#!/usr/bin/python

import argparse
import shutil
import os
import sys
import json
import subprocess
from pathlib import Path
import threading
import select
import graphlib
import re

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from mods.utils import (
    init_or_reset_repo,
    get_target_triple,
    get_arch_flags,
    get_all_arches,
    TqdmProgress,
    is_repo_dirty,
)
from mods.snapshot import (
    take_snapshot,
    compare_snapshots,
    write_report,
    get_report_paths,
)
from mods import colors
from mods.config import (
    set_base_dir,
    load_repo_config,
    scan_repos,
    save_repo_state,
    deduplicate_repos,
    filter_by_requested,
    ConfigError,
)
from mods.deps import (
    get_build_order,
    get_keep_dirs,
    filter_repos_for_build,
    filter_repos_for_sync,
)
from mods.git_ops import (
    sync_repo,
    handle_repos,
    handle_top_level_repo,
    print_top_level_status,
)
from mods.trace import (
    init_trace,
    is_enabled,
    repo_created,
    repo_removed,
    repo_synced,
    build_step_start,
    build_step_end,
    tools_step_start,
    tools_step_end,
    trace_deps,
    trace_file_open,
    trace_file_close,
    trace_exception,
    trace_exit,
)
from mods.emulation import get_qemu_command, add_test_disk, get_console_args, run_qemu

skel_dir = BASE_DIR / "skel"

try:
    import curses

    CURSES_OK = True
except ImportError:
    CURSES_OK = False

import importlib.util


def strip_ansi_codes(text):
    """Remove ANSI escape sequences from text."""
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)


def load_repo_una(repo_dir: str, una_file_name: str = "una.py"):
    """
    Dynamically load the specified una file from the repo directory.
    Defaults to 'una.py'.
    """
    una_file = Path(repo_dir) / una_file_name
    if not una_file.exists():
        colors.error(
            f"Error: {una_file} not found. Build script is missing for this component."
        )
        sys.exit(1)

    # Create a unique module name based on repo name and script name
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
        self.component_snapshots = {}  # name -> {staging: {}, target: {}}
        self.cleaned_components = set()
        # Create build_logs directory at tools level
        self.build_logs_dir = self.bld_base / "build_logs"
        self.build_logs_dir.mkdir(parents=True, exist_ok=True)

    def run_step(self, cfg, step_name, step_func, **kwargs):
        name = cfg["name"]
        colors.info(f"[{self.arch}] Running {name}::{step_name}...")
        if is_enabled():
            build_step_start(self.arch, name, step_name)
        # 1. Cleanup and Pre-snapshot on first call for this component
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

        # 2. Execute step with output capturing to log file
        log_file_path = self.build_logs_dir / f"{name}.txt"
        colors.info(f"[{self.arch}] Build log: {log_file_path}")

        # Notify curses UI of the current log file being built
        if self.curses_ui:
            self.curses_ui.set_current_log(str(log_file_path))

        # Use file descriptor redirection to capture all output (including subprocesses)
        # Save original stdout and stderr file descriptors
        original_stdout_fd = os.dup(1)
        original_stderr_fd = os.dup(2)

        # Save Python-level sys.stdout/stderr objects so they can be restored
        old_sys_stdout = sys.stdout
        old_sys_stderr = sys.stderr

        # Open log file
        trace_file_open(str(log_file_path), "w")
        try:
            log_file = open(log_file_path, "w")
        except Exception as e:
            trace_exception(f"open({log_file_path})", e)
            raise

        # Only use pipe/thread capture if requested (when curses is active)
        # When in non-curses mode, write directly without capture
        # Check the ORIGINAL stdout, not any replaced object that may not have isatty()
        original_stdout_is_tty = old_sys_stdout.isatty() if hasattr(old_sys_stdout, 'isatty') else False
        # Use pipe capture only if explicitly requested AND stdout is not a real terminal
        use_pipe_capture = self.use_pipe_capture and not original_stdout_is_tty

        pipe_read, pipe_write = None, None
        async_top_writer = None
        async_file_writer = None

        if use_pipe_capture:
            # Create a pipe for capturing output
            pipe_read, pipe_write = os.pipe()

            # Redirect stdout and stderr (FD-level) to the pipe
            os.dup2(pipe_write, 1)
            os.dup2(pipe_write, 2)

            # Rebind Python-level sys.stdout/stderr to a Tee so Python prints go to both the
            # original sys.stdout (usually the curses Writer) and the pipe (so reader can log)
            try:
                import io, queue

                # Background asynchronous writer to avoid blocking the main thread
                class AsyncLogWriter:
                    def __init__(self, writer_obj, logfile, write_to_top=True):
                        self.writer = writer_obj
                        self.logfile = logfile
                        self.write_to_top = bool(write_to_top)
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
                            # Strip \r characters before writing (handles both log file and curses Writer)
                            if isinstance(text, str):
                                text = text.replace('\r', '')
                            try:
                                if hasattr(self.logfile, 'write'):
                                    # Strip ANSI codes for log file
                                    clean_text = strip_ansi_codes(text)
                                    self.logfile.write(clean_text)
                                    try:
                                        self.logfile.flush()
                                        # Force sync to disk for immediate visibility
                                        try:
                                            os.fsync(self.logfile.fileno())
                                        except Exception:
                                            pass
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                            # Optionally write to top writer (curses Writer)
                            if self.write_to_top and self.writer is not None:
                                try:
                                    if hasattr(self.writer, 'write'):
                                        self.writer.write(text)
                                        try:
                                            self.writer.flush()
                                        except Exception:
                                            pass
                                except Exception:
                                    # If writing to the curses writer fails, skip writing to terminal
                                    # to avoid emitting raw escape sequences outside curses.
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
                        """Wait for all queued items to be processed."""
                        try:
                            self.q.join()
                        except Exception:
                            pass
                    def stop(self):
                        try:
                            self.q.put(None)
                            self.thread.join(timeout=5)
                        except Exception:
                            pass

                # Writer for top pane (Python prints only)
                async_top_writer = AsyncLogWriter(old_sys_stdout, log_file, write_to_top=True)
                # Writer for subprocess output -> log file only (no curses display)
                async_file_writer = AsyncLogWriter(None, log_file, write_to_top=False)

                class StdoutReplacer:
                    def __init__(self, aw):
                        self.aw = aw
                    def write(self, txt):
                        # Queue writes so the main thread never holds the curses Writer lock
                        try:
                            # Strip \r characters to prevent garbled output
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

                sys.stdout = StdoutReplacer(async_top_writer)
                sys.stderr = sys.stdout
            except Exception:
                # If we can't rebind, continue — reader will capture subprocess output
                pass

        # Buffer for output data
        output_buffer = []
        buffer_lock = threading.Lock()
        stop_event = threading.Event()
        reader = None

        if use_pipe_capture and pipe_read is not None:
            def reader_thread():
                """Read from pipe and enqueue data for the asynchronous writer.

                Blocking read loop. Puts decoded text into async_writer so a single
                background thread performs all writes (avoids writer lock deadlocks).
                """
                try:
                    while True:
                        try:
                            data = os.read(pipe_read, 4096)
                        except (OSError, IOError):
                            break
                        if not data:
                            break
                        text = data.decode('utf-8', errors='replace')
                        try:
                            # Send subprocess/pipe data only to file writer
                            if async_file_writer:
                                async_file_writer.put(text)
                        except Exception:
                            # don't write raw bytes to original stdout (would corrupt terminal)
                            pass
                        with buffer_lock:
                            output_buffer.append(data)
                finally:
                    # Drain any remaining data (best effort)
                    try:
                        while True:
                            data = os.read(pipe_read, 4096)
                            if not data:
                                break
                            text = data.decode('utf-8', errors='replace')
                            try:
                                # Drain remaining data into file-only writer
                                if async_file_writer:
                                    async_file_writer.put(text)
                            except Exception:
                                # don't write raw bytes to original stdout (would corrupt terminal)
                                pass
                            with buffer_lock:
                                output_buffer.append(data)
                    except Exception:
                        pass

            # Start reader thread (non-daemon so we can join reliably)
            reader = threading.Thread(target=reader_thread, daemon=False)
            reader.start()
        else:
            # When not using pipe capture, tee FD-level stdout/stderr to terminal and log file
            pipe_read_fd, pipe_write_fd = os.pipe()
            os.dup2(pipe_write_fd, 1)
            os.dup2(pipe_write_fd, 2)

            def _tee_thread():
                try:
                    while True:
                        data = os.read(pipe_read_fd, 4096)
                        if not data:
                            break
                        os.write(original_stdout_fd, data)
                        log_file.write(data.decode(errors="replace"))
                        log_file.flush()
                except Exception:
                    pass

            tee_thread = threading.Thread(target=_tee_thread, daemon=True)
            tee_thread.start()

        try:
            # Execute the step function
            step_func(self.staging_dir, self.target_dir, **kwargs)
        finally:
            if use_pipe_capture:
                # Stop the reader thread
                stop_event.set()

                # Close the write end of the pipe to signal EOF
                try:
                    os.close(pipe_write)
                except Exception:
                    pass

                # Restore original file descriptors so FD1 no longer points to pipe (allow reader to see EOF)
                try:
                    os.dup2(original_stdout_fd, 1)
                    os.dup2(original_stderr_fd, 2)
                except Exception:
                    pass

                # Wait for reader thread to finish (block until drained)
                if reader:
                    try:
                        reader.join()
                    except Exception:
                        pass
            else:
                # Close pipe_write to signal EOF to tee thread (non-pipe-capture case)
                try:
                    if 'pipe_write_fd' in dir() and pipe_write_fd is not None:
                        os.close(pipe_write_fd)
                except Exception:
                    pass
                # Restore original file descriptors
                try:
                    os.dup2(original_stdout_fd, 1)
                    os.dup2(original_stderr_fd, 2)
                except Exception:
                    pass
                # Wait for tee thread to finish draining remaining data
                try:
                    if 'tee_thread' in dir() and tee_thread is not None:
                        tee_thread.join(timeout=5)
                except Exception:
                    pass

            # Restore Python-level stdout/stderr if we changed them
            try:
                # If we set sys.stdout to a Tee or pipe wrapper, restore originals
                if 'old_sys_stdout' in locals() and old_sys_stdout is not None:
                    sys.stdout = old_sys_stdout
                if 'old_sys_stderr' in locals() and old_sys_stderr is not None:
                    sys.stderr = old_sys_stderr
            except Exception:
                pass

            # Close our duplicates
            try:
                os.close(original_stdout_fd)
            except Exception:
                pass
            try:
                os.close(original_stderr_fd)
            except Exception:
                pass
            if pipe_read:
                try:
                    os.close(pipe_read)
                except Exception:
                    pass

            # Ensure asynchronous writers have flushed pending writes
            if async_top_writer:
                try:
                    async_top_writer.stop()
                except Exception:
                    pass
            if async_file_writer:
                try:
                    async_file_writer.stop()
                except Exception:
                    pass

            # Close log file
            try:
                trace_file_close(str(log_file_path))
                log_file.close()
            except Exception as e:
                trace_exception(f"close({log_file_path})", e)
                pass

        # 3. Post-snapshot and report
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

        # Compile combined report
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


def sync_kernel_config(src: Path, dest: Path):
    """
    Syncs back the updated kernel config, stripping leading comments
    and forcing specific values like CONFIG_CC_VERSION_TEXT.
    """
    if not src.exists():
        return
    content = src.read_text()
    lines = content.splitlines()
    out_lines = []

    # Skip leading comments and empty lines
    header_done = False
    for line in lines:
        if not header_done:
            if line.strip().startswith("#") or not line.strip():
                continue
            else:
                header_done = True

        # Process entries
        if line.startswith("CONFIG_CC_VERSION_TEXT="):
            out_lines.append('CONFIG_CC_VERSION_TEXT="clang"')
        else:
            out_lines.append(line)

    dest.write_text("\n".join(out_lines) + "\n")


def list_repos(repos, target_type=None):
    """
    Helper function to filter and print repo directories by type.
    If target_type is None, prints all.
    """
    if target_type == "target":
        filtered = [
            r
            for r in repos
            if r.get("type") != "tools"
            and not r.get("is_virtual")
            and r.get("type") != "virtual"
        ]
    else:
        filtered = [
            r
            for r in repos
            if (target_type is None or r.get("type") == target_type)
            and not r.get("is_virtual")
            and r.get("type") != "virtual"
        ]
    for r in filtered:
        script_info = f" (Script: {r.get('una_file', 'una.py')})"
        print(
            f"[{r.get('type', 'unknown')}] {r['name']} -> {r['repo_dir']}{script_info}"
        )
    return [r["name"] for r in filtered]


def get_git_remote_base():
    """
    Attempts to determine the base URL of the current git repository's remote.
    Specifically looks for a remote named 'una'.
    """
    try:
        from git import Repo

        # Use the absolute path of the script's directory for more reliable repo discovery
        script_dir = Path(__file__).resolve().parent
        repo = Repo(script_dir, search_parent_directories=True)

        # Find the remote named 'una' explicitly
        for r in repo.remotes:
            if r.name == "una":
                url = str(r.url)
                if "/" in url:
                    return url.rsplit("/", 1)[0]
    except Exception:
        pass
    return None


def create_test_disk(disk_path):
    if disk_path.exists():
        print(f"Test disk {disk_path} already exists. Skipping creation.")
        return

    print(f"Creating 1G test disk at {disk_path}...")

    # 1. Create 1G raw image
    subprocess.run(
        ["qemu-img", "create", "-f", "raw", str(disk_path), "1G"], check=True
    )

    # 2. Partition with sgdisk
    # Alignment=1 to allow sector 3. Table size reduced to 4 entries to fit starting at sector 3.
    subprocess.run(
        ["sgdisk", "--set-alignment=1", "--resize-table=4", str(disk_path)], check=True
    )
    subprocess.run(
        ["sgdisk", "--set-alignment=1", "--new=1:3:65365", str(disk_path)], check=True
    )
    subprocess.run(["sgdisk", "--typecode=1:ef00", str(disk_path)], check=True)
    subprocess.run(
        ["sgdisk", "--set-alignment=1", "--new=2:65536:0", str(disk_path)], check=True
    )
    subprocess.run(["sgdisk", "--typecode=2:8300", str(disk_path)], check=True)

    # 3. Format Partitions
    p1_sectors = 65365 - 3 + 1
    p1_size = p1_sectors * 512

    # Calculate P2 size. 1G = 2097152 sectors.
    # We find the actual last sector from sgdisk or just assume 1G minus GPT overhead.
    total_sectors = 1024 * 1024 * 1024 // 512
    p2_sectors = total_sectors - 65536 - 34  # 34 for the backup GPT at the end
    p2_size = p2_sectors * 512

    p1_img = disk_path.with_suffix(".p1.tmp")
    p2_img = disk_path.with_suffix(".p2.tmp")

    try:
        # Format P1 (FAT16 for EFI)
        print("Formatting Partition 1 (FAT16)...")
        p1_img.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["truncate", "-s", str(p1_size), str(p1_img)], check=True)
        subprocess.run(
            ["mkfs.fat", "-f1", "-F16", "-n", "BOOT0EFI", str(p1_img)], check=True
        )
        subprocess.run(
            [
                "dd",
                f"if={p1_img}",
                f"of={disk_path}",
                "bs=512",
                "seek=3",
                "conv=notrunc",
            ],
            check=True,
        )

        # Format P2 (EXT4)
        print("Formatting Partition 2 (EXT4)...")
        subprocess.run(["truncate", "-s", str(p2_size), str(p2_img)], check=True)
        subprocess.run(["mkfs.ext4", "-F", str(p2_img)], check=True)
        subprocess.run(
            [
                "dd",
                f"if={p2_img}",
                f"of={disk_path}",
                "bs=512",
                "seek=65536",
                "conv=notrunc",
            ],
            check=True,
        )

        print("Test disk created successfully.")
    except Exception as e:
        colors.error(f"Error creating test disk: {e}")
        if disk_path.exists():
            disk_path.unlink()
        raise
    finally:
        if p1_img.exists():
            p1_img.unlink()
        if p2_img.exists():
            p2_img.unlink()


def propagate_skel(staging_dir, target_dir):
    """Skel propagation using original file-by-file method + snapshot verification"""

    colors.info("Propagating skeleton (original method)...")

    for dest in [staging_dir, target_dir]:
        # ORIGINAL logic: Handle ONLY symlink/dir conflicts
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

        # ORIGINAL cp -a --remove-destination (robust merge)
        subprocess.run(
            ["cp", "-a", "--remove-destination", f"{skel_dir}/.", str(dest)], check=True
        )


def remove_repo(name, repos, arches, bld_base):
    """Removes a repository from the list, cleans build outputs and deletes repo dir."""
    target = next((r for r in repos if r["name"] == name), None)
    if not target:
        colors.error(f"Error: Repository '{name}' not found.")
        return False

    colors.info(f"Removing repository '{name}'...")

    # 1. Clean build outputs for each architecture
    for arch in arches:
        report_file = bld_base / arch / "report" / f"{name}.txt"
        if report_file.exists():
            colors.info(f"[{arch}] Cleaning build outputs for {name}...")
            paths = get_report_paths(report_file)
            staging_dir = bld_base / "staging"
            target_dir = bld_base / "target"

            for p in paths:
                try:
                    if p.startswith("staging/"):
                        (staging_dir / p[8:]).unlink(missing_ok=True)
                    elif p.startswith("target/"):
                        (target_dir / p[7:]).unlink(missing_ok=True)
                except Exception as e:
                    colors.warn(f"[{arch}] Warning: Failed to remove {p}: {e}")
            report_file.unlink()

    # 2. Delete the repository directory
    repo_dir = Path(target["repo_dir"])
    if repo_dir.exists():
        print(f"Deleting repository directory: {repo_dir}")
        if is_enabled():
            repo_removed(name, repo_dir)
        shutil.rmtree(repo_dir)

    # 3. Remove from repos list to prevent sync attempts
    repos[:] = [r for r in repos if r["name"] != name]

    colors.info(f"Repository '{name}' removed successfully.")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Initialize or reset Git repos from a list.",
    )
    git_base = get_git_remote_base()
    parser.add_argument(
        "--list",
        choices=["tools", "target", "all"],
        help="List repos of the specified type.",
    )
    parser.add_argument(
        "--build",
        nargs="*",
        help="Build specific component(s) by name. If no name is provided, build all components.",
    )
    parser.add_argument(
        "--rebase",
        nargs=2,
        metavar=("component", "new_tag"),
        help=(
            "Rebase a component's patches onto a new upstream tag. "
            "Finds the fork-point from the current tag, rebases onto new_tag, "
            "updates the tag in the .repo file, then shows a diff stat."
        ),
    )

    parser.add_argument(
        "--run",
        action="store_true",
        help="Run the default kernel using emulation for specified architecture.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show git status for the top-level repo and all sub-repositories.",
    )
    parser.add_argument(
        "--save",
        metavar="tag",
        help="Stage all changes, commit with the provided message, then rebase (with squash) and push for all repositories.",
    )
    parser.add_argument(
        "--create-disk",
        action="store_true",
        help="Create a shared 1G test disk in the bld directory.",
    )
    parser.add_argument(
        "--checkout",
        metavar="tag",
        help="Checkout a specific tag in all repositories.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove all files produced by the build and clean the workspace repositories.",
    )
    parser.add_argument(
        "--conf",
        help="Path to the repository configuration file(s), comma-separated.",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Generate a summary of changes in the repositories.",
    )
    parser.add_argument(
        "--no-curses",
        action="store_true",
        help="Disable curses split-screen display (curses is enabled by default).",
    )
    parser.add_argument(
        "--trace",
        metavar="FILE",
        help="Trace repo and build operations to specified file (overwrites if exists).",
    )

    args = parser.parse_args()

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    if args.trace:
        init_trace(args.trace)

    if not args.conf:
        if args.status or args.create_disk:
            pass # --status can run without a conf file
        else:
            colors.error("Error: --conf is required.")
            sys.exit(1)

    base = BASE_DIR / "bld"
    tmp = base / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)

    keep_list = {"PATH", "LANG"}

    global_env = {k: os.environ[k] for k in keep_list if k in os.environ}
    global_env["HOME"] = str(base)
    global_env["TMPDIR"] = str(tmp)

    os.environ.clear()
    os.environ.update(global_env)

    conf_files = [c.strip() for c in args.conf.split(",")] if args.conf else []

    if args.run and len(conf_files) > 1:
        colors.error("Error: --run requires exactly one configuration file.")
        sys.exit(1)

    # Validate --run usage: only with --build (optional) and no other commands
    if args.run:
        conflicting_commands = [
            ('--list', args.list),
            ('--status', args.status),
            ('--rebase', args.rebase),
            ('--save', args.save),
            ('--checkout', args.checkout),
            ('--clean', args.clean),
            ('--create-disk', args.create_disk),
            ('--report', args.report),
        ]
        
        conflicting = [cmd for cmd, val in conflicting_commands if val]
        if conflicting:
            colors.error(f"Error: --run cannot be combined with {', '.join(conflicting)}.")
            sys.exit(1)

    if args.status:
        from mods.git_ops import print_top_level_status, handle_repos
        print_top_level_status(BASE_DIR)
        
        repos = []
        repo_base = BASE_DIR / "repo"
        if repo_base.exists():
            # Find all directories containing a .git folder or file
            for git_path in repo_base.rglob(".git"):
                repo_path = git_path.parent
                repos.append({"name": repo_path.name, "repo_dir": str(repo_path)})
        
        repos.sort(key=lambda r: r["name"])
        handle_repos(repos, "status")
        sys.exit(0)

    test_disk = BASE_DIR / "bld" / "test.img"
    if args.create_disk:
        create_test_disk(test_disk)
        sys.exit(0)

    conf_name = Path(conf_files[0]).stem
    bld_base = BASE_DIR / "bld" / conf_name
    tools_install_dir = BASE_DIR / "bld" / "tools"
    repos_config = []
    global_cfg = {}

    set_base_dir(BASE_DIR)

    for conf_file in conf_files:
        conf_path = BASE_DIR / conf_file
        if not conf_path.exists():
            colors.error(f"Error: Config file {conf_path} not found.")
            sys.exit(1)

        rc, gc = load_repo_config(conf_path)

        repos_config.extend(rc)

        if not global_cfg:
            global_cfg = gc
        else:
            for k, v in gc.items():
                if k not in global_cfg:
                    global_cfg[k] = v

    arches = []
    if "arch" in global_cfg:
        arch = global_cfg["arch"].strip()
        if " " in arch:
            raise ConfigError(f"Architecture must be a single value, not: '{arch}'")
        if arch not in get_all_arches():
            raise ConfigError(f"Architecture must be one of {get_all_arches()}, got: '{arch}'")
        arches = [arch]
    if not arches:
        raise ConfigError(f"Architecture must be specified. Valid options: {get_all_arches()}")

    # Deduplicate repos_config
    repos_config = deduplicate_repos(repos_config)
    for cfg in repos_config:
        cfg["is_virtual"] = cfg.get("type") == "virtual"

    una_base = get_git_remote_base()

    repos = filter_repos_for_sync(repos_config)
    for r in repos:
        if una_base:
            base = una_base
            if not base.endswith("/") and not base.endswith(":"):
                base += "/"
            r["una_url"] = f"{base}{r['una_repo']}"
        else:
            r["una_url"] = "UNKNOWN_BASE"

    build_all = False
    if args.build is not None and len(args.build) == 0:
        if "components" in global_cfg:
            args.build = [
                c.strip()
                for c in global_cfg["components"].replace(",", " ").split()
                if c.strip()
            ]
        else:
            build_all = True

    if args.build is not None and not build_all:
        required_names = set(args.build)
    else:
        required_names = {r["name"] for r in repos_config}

    filtered_repos = filter_by_requested(repos_config, required_names)

    try:
        build_order, dep_graph = get_build_order(filtered_repos, required_names)
    except ConfigError as e:
        colors.error(f"Dependency error: {e}")
        sys.exit(1)

    if is_enabled():
        trace_deps(build_order, dep_graph)

    keep_repo_dirs = get_keep_dirs(repos_config, dep_graph)

    repos_to_sync = {r["name"] for r in filtered_repos}

    # Prepare git log directory for pre-build git operations
    git_logs_dir = bld_base / "git_logs"
    git_logs_dir.mkdir(parents=True, exist_ok=True)

    for cfg in repos_config:
        if cfg.get("is_virtual") or cfg["name"] not in repos_to_sync:
            continue
        if "repo_dir" not in cfg:
            continue

        # Redirect git output to component_git_pre.txt
        git_log_file = git_logs_dir / f"{cfg['name']}_git_pre.txt"
        original_stdout_fd = os.dup(1)
        original_stderr_fd = os.dup(2)
        try:
            colors.info(f">>> Syncing repo: {cfg['name']} (output -> {git_log_file})")
            with open(git_log_file, "w") as f:
                os.dup2(f.fileno(), 1)
                os.dup2(f.fileno(), 2)
                if sync_repo(cfg, una_base):
                    save_repo_state(cfg)
        finally:
            os.dup2(original_stdout_fd, 1)
            os.dup2(original_stderr_fd, 2)
            os.close(original_stdout_fd)
            os.close(original_stderr_fd)

    # Cleanup AFTER sync - remove repos not in current config
    # First, add any newly synced repos to valid dirs
    valid_repo_dirs = keep_repo_dirs.copy()
    for cfg in repos_config:
        if cfg.get("is_virtual") or "repo_dir" not in cfg:
            continue
        if cfg["name"] in repos_to_sync:
            valid_repo_dirs.add(Path(cfg["repo_dir"]).absolute())

    # Remove repos from scanned list that are not in valid_repo_dirs
    scanned = scan_repos()
    for s_cfg in scanned:
        if Path(s_cfg["repo_dir"]).absolute() not in valid_repo_dirs:
            colors.warn(
                f"Repository '{s_cfg['name']}' found in repo/ but not required by config. Removing..."
            )
            remove_repo(s_cfg["name"], repos_config, arches, bld_base)

    # Clean repos in filesystem that are not in valid_repo_dirs (including git repos)
    repo_base = BASE_DIR / "repo"
    if repo_base.exists():
        for d in repo_base.iterdir():
            if d.is_dir() and d.absolute() not in valid_repo_dirs:
                colors.warn(
                    f"Repository '{d.name}' exists in repo/ but not required by config. Removing..."
                )
                shutil.rmtree(d, ignore_errors=True)

    repos_to_process = []
    repos_by_name = {r["name"]: r for r in repos}
    for name in build_order:
        if name in repos_by_name:
            repos_to_process.append(repos_by_name[name])

    # Check if repos exist before building or rebasing
    if args.build is not None or args.rebase:
        missing = [
            r["name"]
            for r in repos_to_process
            if not r.get("is_virtual")
            and "repo_dir" in r
            and not Path(r["repo_dir"]).exists()
        ]
        if missing:
            colors.warn(
                f"Warning: The following repository directories are missing: {', '.join(missing)}"
            )
            print(
                "These should have been initialized automagically if a base URL was available."
            )
            sys.exit(1)

    if args.list:
        target_type = None if args.list == "all" else args.list
        list_repos(repos, target_type)
        return


    if args.build is not None or build_all:
        # Debug: report why curses UI will or won't be used
        try:
            stderr_msg = (
                f"DEBUG: no_curses={args.no_curses}, stdout_isatty={sys.stdout.isatty()}, TERM={os.environ.get('TERM')}, arches={arches}\n"
            )
            sys.stderr.write(stderr_msg)
            sys.stderr.flush()
        except Exception:
            pass

        # Try curses mode, fall back to non-curses if it fails
        use_curses = not args.no_curses and sys.stdout.isatty()
        build_result = False

        if use_curses:
            try:
                from mods.curses_ui import CursesUI
                from mods.build import init_build, run_build

                # Prepare git log directory for pre-build git operations
                git_logs_dir = bld_base / "git_logs"
                git_logs_dir.mkdir(parents=True, exist_ok=True)

                # Determine log_dir
                conf_name = Path(conf_files[0]).stem
                log_dir = str(BASE_DIR / "bld" / conf_name / "build_logs")

                # Debug: indicate starting curses UI and the log_dir
                try:
                    sys.stderr.write(f"DEBUG: Starting CursesUI with log_dir={log_dir}\n")
                    sys.stderr.flush()
                except Exception:
                    pass

                ui = CursesUI(log_dir=log_dir)
                # Initialize build module with curses_ui instance
                init_build(
                    colors,
                    load_repo_una,
                    StepRunner,
                    get_target_triple,
                    get_arch_flags,
                    propagate_skel,
                    sync_kernel_config,
                    is_repo_dirty,
                    BASE_DIR,
                    bld_base,
                    arches,
                    repos,
                    repos_to_process,
                    required_names,
                    build_all,
                    tools_install_dir,
                    skel_dir,
                    global_cfg,
                    use_curses,
                    git_logs_dir,
                    ui,
                )
                # Pass the build function to run in background and capture result
                build_result = ui.start(run_build, args)
                
                # If --run is specified and build succeeded, continue to run; otherwise exit
                if args.run:
                    if not build_result:
                        colors.error("Build failed. Skipping --run.")
                        sys.exit(1)
                    # Fall through to run stage below
                else:
                    return
            except ImportError:
                colors.error("Error: curses not available")
                sys.exit(1)
            except Exception as e:
                colors.warn(
                    f"Warning: curses UI failed ({e}), falling back to non-curses mode"
                )
                # Mark curses as failed so StepRunner doesn't use pipe capture
                use_curses = False
                # Restore terminal in case curses partially initialized
                try:
                    import curses
                    curses.echo()
                    curses.nocbreak()
                    curses.endwin()
                except:
                    pass
                # Reset terminal to fix any remaining issues
                try:
                    subprocess.run(['stty', 'sane'], check=False)
                except:
                    pass
                # Fall through to non-curses mode below

        # Run build directly (no curses)
        from mods.build import init_build, run_build

        init_build(
            colors,
            load_repo_una,
            StepRunner,
            get_target_triple,
            get_arch_flags,
            propagate_skel,
            sync_kernel_config,
            is_repo_dirty,
            BASE_DIR,
            bld_base,
            arches,
            repos,
            repos_to_process,
            required_names,
            build_all,
            tools_install_dir,
            skel_dir,
            global_cfg,
            use_curses,
            git_logs_dir,
        )
        build_result = run_build(args)
        
        # If --run is specified and build succeeded, continue to run; otherwise exit
        if args.run:
            if not build_result:
                colors.error("Build failed. Skipping --run.")
                sys.exit(1)
            # Fall through to run stage below
        else:
            return

    if args.run:
        target_name = "linux-image"
        proj = next((r for r in repos if r["name"] == target_name), None)
        if not proj:
            print(f"Error: Component '{target_name}' not found.")
            sys.exit(1)

        if len(arches) > 1:
            print("Error: --run only supports one architecture at a time.")
            sys.exit(1)

        arch = arches[0]
        print(f"\n--- Run Stage: {target_name} ({arch}) ---")

        kernel_name = global_cfg.get("kernel_name", "kernel")
        kernel_img = bld_base / kernel_name
        if not kernel_img.exists():
            print(
                f"Error: Kernel image not found at {kernel_img}. Please build it first with --build {target_name}."
            )
            sys.exit(1)

        try:
            cmd = get_qemu_command(arch, kernel_img, get_console_args(arch))
        except ValueError as e:
            print(f"Error: {e}")
            sys.exit(1)

        cmd = add_test_disk(cmd, bld_base / "test.img")

        try:
            run_qemu(cmd)
        except KeyboardInterrupt:
            print("\nKernel execution stopped by user.")
            sys.exit(1)

    tag = args.save
    if tag:
        action = "save"
        handle_top_level_repo(BASE_DIR, action, tag, squash=True)
        handle_repos(repos_to_process, action, tag, include_all=False)

    if args.rebase:
        comp_name, new_tag = args.rebase
        from mods.git_ops import rebase_to_tag
        rebase_to_tag(
            comp_name=comp_name,
            new_tag=new_tag,
            repos_config=repos_config,
            base_dir=BASE_DIR,
        )

    if args.checkout:
        handle_top_level_repo(BASE_DIR, "checkout", args.checkout, squash=True)
        handle_repos(repos_to_process, "checkout", args.checkout)

    if args.clean:
        print("\n=== Global Cleanup ===")

        # 1. Clean build directory (staging and target, but keep reports?)
        # User said "removes all files produced by the build".
        # Reports are also produced by the build but useful for next build cleanup.
        # Let's keep 'report' directory but clean staging/target.
        for arch in arches:
            staging_dir = bld_base / "staging"
            target_dir = bld_base / "target"

            if staging_dir.exists():
                print(f"Cleaning {staging_dir}...")
                shutil.rmtree(staging_dir)
                staging_dir.mkdir(parents=True, exist_ok=True)
            if target_dir.exists():
                print(f"Cleaning {target_dir}...")
                shutil.rmtree(target_dir)
                target_dir.mkdir(parents=True, exist_ok=True)

        # 1.5 Clean tools build artifacts and state file so tools will be rebuilt
        tools_install_dir = BASE_DIR / "bld" / "tools"
        tools_state_file = tools_install_dir / "tools_state"
        
        if tools_install_dir.exists():
            print(f"Cleaning {tools_install_dir}...")
            shutil.rmtree(tools_install_dir)
            tools_install_dir.mkdir(parents=True, exist_ok=True)

        # 2. Clean workspace sub-repos
        cleaned_dirs = set()
        for r in repos:
            # Skip virtual components that don't have repo_dir
            if r.get("is_virtual") or r.get("type") == "virtual" or "repo_dir" not in r:
                continue
            r_path = Path(r["repo_dir"]).absolute()
            if r_path in cleaned_dirs:
                continue
            if r_path.exists() and (r_path / ".git").exists():
                print(f"Cleaning {r['name']} ({r['repo_dir']})...")
                if is_repo_dirty(r_path):
                    print(
                        f"ERROR: Repository {r['name']} is dirty. Stopping global cleanup."
                    )
                    sys.exit(1)
                if is_enabled():
                    from mods.trace import repo_cleaned
                    repo_cleaned(Path(r_path))
                print(f">>> git clean -fdx -q  (in {r_path})")
                subprocess.run(["git", "clean", "-fdx", "-q"], cwd=r_path, check=True)
                cleaned_dirs.add(r_path)

        # 2.5 Clean unreferenced repos in repo/
        repo_base = BASE_DIR / "repo"
        if repo_base.exists():
            valid_repo_dirs = {
                Path(r["repo_dir"]).absolute()
                for r in repos
                if not r.get("is_virtual") and "repo_dir" in r
            }
            for d in repo_base.iterdir():
                if d.is_dir() and d.absolute() not in valid_repo_dirs:
                    print(f"Removing unreferenced directory {d}...")
                    shutil.rmtree(d, ignore_errors=True)

        # 3. Clean top-level workspace (excluding reports, kernel images, and repos)
        print("Cleaning top-level workspace...")
        if is_repo_dirty(BASE_DIR):
            print("ERROR: Top-level repository is dirty. Stopping global cleanup.")
            sys.exit(1)
        print(f">>> git clean -xfd -e bld/ -e repo/ -q  (in {BASE_DIR})")
        subprocess.run(
            ["git", "clean", "-xfd", "-e", "bld/", "-e", "repo/", "-q"], cwd=BASE_DIR
        )

    if args.report:
        from git import Repo

        print("\n=== Generating Change Report ===")
        report_dir = bld_base / "report"
        report_dir.mkdir(parents=True, exist_ok=True)

        summary_file = report_dir / "summary.txt"
        summary_lines = []

        processed_dirs = set()

        # Include all repositories from configuration
        all_repos_to_report = repos

        for cfg in all_repos_to_report:
            r_path = Path(cfg["repo_dir"]).absolute()
            if r_path in processed_dirs:
                continue
            if not r_path.exists() or not (r_path / ".git").exists():
                continue

            repo = Repo(r_path)
            name = cfg["name"]

            # Determine target branch/tag (consistent with rebase logic)
            remote_prefix = "origin" if "origin_url" in cfg else "una"
            branch = cfg.get("branch")
            tag_name = cfg.get("tag")
            if branch:
                target = f"{remote_prefix}/{branch}"
            elif tag_name:
                target = tag_name
            else:
                target = f"{remote_prefix}/{get_remote_head(repo, remote_prefix)}"

            print(f"[{name}] Comparing against {target}...")

            try:
                # 1. Get diffstat
                diffstat = repo.git.diff(target, "--stat")
                # 2. Get full diff
                full_diff = repo.git.diff(target)

                # If there are no changes, we still record it
                if not diffstat.strip():
                    diffstat = "No changes."
                    full_diff = ""

                # Write full diff to a file
                diff_filename = f"{name}.diff"
                diff_file = report_dir / diff_filename
                diff_file.write_text(full_diff)

                # Add to summary
                summary_lines.append(f"Repository: {name}")
                summary_lines.append(f"Base:       {target}")
                summary_lines.append(f"Full Diff:  {diff_filename}")
                summary_lines.append("Diff Stat:")
                summary_lines.append(diffstat)
                summary_lines.append("-" * 60)
                summary_lines.append("")

            except Exception as e:
                print(f"[{name}] Error generating diff: {e}")
                summary_lines.append(f"Repository: {name}")
                summary_lines.append(f"Error: {e}")
                summary_lines.append("-" * 60)
                summary_lines.append("")

            processed_dirs.add(r_path)

        summary_file.write_text("\n".join(summary_lines))
        print(f"\nReport complete. Summary written to: {summary_file}")


if __name__ == "__main__":
    main()
