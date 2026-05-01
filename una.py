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
)
from mods.emulation import get_qemu_command, add_test_disk, get_console_args, run_qemu

skel_dir = BASE_DIR / "skel"

try:
    import curses

    CURSES_OK = True
except ImportError:
    CURSES_OK = False

import importlib.util


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
    def __init__(self, arch, staging_dir, target_dir, bld_base):
        self.arch = arch
        self.staging_dir = staging_dir
        self.target_dir = target_dir
        self.bld_base = bld_base
        self.component_snapshots = {}  # name -> {staging: {}, target: {}}
        self.cleaned_components = set()
        # Create build_logs directory at same level as report
        self.build_logs_dir = self.bld_base / self.arch / "build_logs"
        self.build_logs_dir.mkdir(parents=True, exist_ok=True)

    def run_step(self, cfg, step_name, step_func, **kwargs):
        name = cfg["name"]
        colors.info(f"[{self.arch}] Running {name}::{step_name}...")
        if is_enabled():
            build_step_start(self.arch, name, step_name)

        # 1. Cleanup and Pre-snapshot on first call for this component
        if name not in self.cleaned_components:
            report_file = self.bld_base / self.arch / "report" / f"{name}.txt"
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

        # Use file descriptor redirection to capture all output (including subprocesses)
        # Save original stdout and stderr file descriptors
        original_stdout_fd = os.dup(1)
        original_stderr_fd = os.dup(2)

        # Open log file
        log_file = open(log_file_path, "w")

        # Create a pipe for capturing output
        pipe_read, pipe_write = os.pipe()

        # Redirect stdout and stderr to the pipe
        os.dup2(pipe_write, 1)
        os.dup2(pipe_write, 2)

        # Buffer for output data
        output_buffer = []
        buffer_lock = threading.Lock()
        stop_event = threading.Event()

        def reader_thread():
            """Read from pipe and write to terminal and log file."""
            # Use a polling approach for cross-platform compatibility
            while not stop_event.is_set():
                try:
                    # Check if there's data to read (with timeout)
                    ready, _, _ = select.select([pipe_read], [], [], 0.1)
                    if ready:
                        data = os.read(pipe_read, 4096)
                        if not data:
                            break
                        # Prefer writing directly to curses Writer window if present
                        written_to_curses = False
                        try:
                            sd = sys.stdout
                            # If sys.stdout looks like the Writer (has win and lock), write directly
                            if hasattr(sd, 'win') and hasattr(sd, 'lock'):
                                try:
                                    text = data.decode('utf-8', errors='replace')
                                    with sd.lock:
                                        try:
                                            h, w = sd.win.getmaxyx()
                                        except Exception:
                                            h, w = (0, 0)
                                        # naive split lines and write
                                        for line in text.split('\n'):
                                            try:
                                                y, x = sd.win.getyx()
                                            except Exception:
                                                y, x = (0, 0)
                                            if line:
                                                truncated = line[: max(w - 1, 0)]
                                                try:
                                                    sd.win.addstr(y, 0, truncated)
                                                    sd.win.clrtoeol()
                                                except Exception:
                                                    pass
                                                # Move to next line
                                                try:
                                                    if y < h - 1:
                                                        sd.win.move(y + 1, 0)
                                                    else:
                                                        sd.win.scroll()
                                                        sd.win.move(max(h - 2, 0), 0)
                                                except Exception:
                                                    pass
                                            else:
                                                try:
                                                    if y < h - 1:
                                                        sd.win.move(y + 1, 0)
                                                    else:
                                                        sd.win.scroll()
                                                        sd.win.move(max(h - 2, 0), 0)
                                                except Exception:
                                                    pass
                                        try:
                                            sd.win.refresh()
                                        except Exception:
                                            pass
                                    written_to_curses = True
                                except Exception:
                                    written_to_curses = False
                            else:
                                # Fallback: attempt safe sys.stdout.write
                                try:
                                    fil = None
                                    try:
                                        fil = sd.fileno()
                                    except Exception:
                                        fil = None
                                    if fil is not None and fil == pipe_write:
                                        raise Exception('stdout is pipe')
                                    sd.write(data.decode('utf-8', errors='replace'))
                                    sd.flush()
                                except Exception:
                                    os.write(original_stdout_fd, data)
                        except Exception:
                            try:
                                os.write(original_stdout_fd, data)
                            except Exception:
                                pass

                        # Write to log file (always try)
                        try:
                            log_file.write(data.decode('utf-8', errors='replace'))
                            log_file.flush()
                        except ValueError:
                            # Log file closed by main thread; exit reader
                            break
                        with buffer_lock:
                            output_buffer.append(data)
                except (OSError, IOError):
                    break

            # Drain remaining data
            try:
                while True:
                    data = os.read(pipe_read, 4096)
                    if not data:
                        break
                    # Drain remaining data; use same direct-to-curses logic as above
                    try:
                        sd = sys.stdout
                        if hasattr(sd, 'win') and hasattr(sd, 'lock'):
                            try:
                                text = data.decode('utf-8', errors='replace')
                                with sd.lock:
                                    try:
                                        h, w = sd.win.getmaxyx()
                                    except Exception:
                                        h, w = (0, 0)
                                    for line in text.split('\n'):
                                        try:
                                            y, x = sd.win.getyx()
                                        except Exception:
                                            y, x = (0, 0)
                                        if line:
                                            truncated = line[: max(w - 1, 0)]
                                            try:
                                                sd.win.addstr(y, 0, truncated)
                                                sd.win.clrtoeol()
                                            except Exception:
                                                pass
                                            try:
                                                if y < h - 1:
                                                    sd.win.move(y + 1, 0)
                                                else:
                                                    sd.win.scroll()
                                                    sd.win.move(max(h - 2, 0), 0)
                                            except Exception:
                                                pass
                                        else:
                                            try:
                                                if y < h - 1:
                                                    sd.win.move(y + 1, 0)
                                                else:
                                                    sd.win.scroll()
                                                    sd.win.move(max(h - 2, 0), 0)
                                            except Exception:
                                                pass
                                    try:
                                        sd.win.refresh()
                                    except Exception:
                                        pass
                            except Exception:
                                try:
                                    os.write(original_stdout_fd, data)
                                except Exception:
                                    pass
                        else:
                            try:
                                fil = None
                                try:
                                    fil = sd.fileno()
                                except Exception:
                                    fil = None
                                if fil is not None and fil == pipe_write:
                                    raise Exception('stdout is pipe')
                                sd.write(data.decode('utf-8', errors='replace'))
                                sd.flush()
                            except Exception:
                                os.write(original_stdout_fd, data)
                    except Exception:
                        try:
                            os.write(original_stdout_fd, data)
                        except Exception:
                            pass

                    try:
                        log_file.write(data.decode('utf-8', errors='replace'))
                        log_file.flush()
                    except ValueError:
                        break
                    with buffer_lock:
                        output_buffer.append(data)
            except (OSError, IOError):
                pass

        # Start reader thread (non-daemon so we can join reliably)
        reader = threading.Thread(target=reader_thread, daemon=False)
        reader.start()

        try:
            # Execute the step function
            step_func(self.staging_dir, self.target_dir, **kwargs)
        finally:
            # Stop the reader thread
            stop_event.set()

            # Close the write end of the pipe to signal EOF
            try:
                os.close(pipe_write)
            except Exception:
                pass

            # Wait for reader thread to finish (block until drained)
            try:
                reader.join()
            except Exception:
                pass

            # Restore original file descriptors
            try:
                os.dup2(original_stdout_fd, 1)
                os.dup2(original_stderr_fd, 2)
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
            try:
                os.close(pipe_read)
            except Exception:
                pass

            # Close log file
            try:
                log_file.close()
            except Exception:
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

        report_file = self.bld_base / self.arch / "report" / f"{name}.txt"
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
    import subprocess

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
    import subprocess

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
            staging_dir = bld_base / arch / "staging"
            target_dir = bld_base / arch / "target"

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
        nargs="?",
        const="ALL",
        help="Rebase the local branch onto the upstream branch (with squash) and push to una. Optional: specify a single repo name.",
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

    if args.trace:
        init_trace(args.trace)

    if not args.conf:
        colors.error("Error: --conf is required.")
        sys.exit(1)

    conf_files = [c.strip() for c in args.conf.split(",") if c.strip()]

    if args.run and len(conf_files) > 1:
        colors.error("Error: --run requires exactly one configuration file.")
        sys.exit(1)

    conf_name = Path(conf_files[0]).stem
    bld_base = BASE_DIR / "bld" / conf_name
    tools_install_dir = BASE_DIR / "bld" / "tools"
    test_disk = bld_base / "test.img"

    if args.create_disk:
        create_test_disk(test_disk)

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

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

    for cfg in repos_config:
        if cfg.get("is_virtual") or cfg["name"] not in repos_to_sync:
            continue
        if "repo_dir" not in cfg:
            continue

        if sync_repo(cfg, una_base):
            save_repo_state(cfg)

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

    if args.status:
        print_top_level_status(BASE_DIR)
        handle_repos(repos, "status")

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

        if not args.no_curses and sys.stdout.isatty():
            try:
                from mods.curses_ui import CursesUI
                from mods.build import init_build, run_build

                # Initialize build module
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
                )
                # Determine log_dir from conf name
                conf_name = Path(conf_files[0]).stem
                arch_for_logs = arches[0] if arches else "x32"
                log_dir = str(BASE_DIR / "bld" / conf_name / arch_for_logs / "build_logs")

                # Debug: indicate starting curses UI and the log_dir
                try:
                    sys.stderr.write(f"DEBUG: Starting CursesUI with log_dir={log_dir}\n")
                    sys.stderr.flush()
                except Exception:
                    pass

                ui = CursesUI(log_dir=log_dir)
                # Pass the build function to run in background
                ui.start(run_build, args)
                return
            except ImportError:
                colors.error("Error: curses not available")
                sys.exit(1)
            except Exception as e:
                colors.warn(
                    f"Warning: curses UI failed ({e}), falling back to non-curses mode"
                )
                # Restore terminal in case curses partially initialized
                try:
                    import curses
                    curses.echo()
                    curses.nocbreak()
                    curses.endwin()
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
        )
        run_build(args)
        return

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
            target_requires_tools = any(
                r.get("type") != "tools" for r in repos_to_process
            )
            if target_requires_tools:
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
            for r in tools_to_build:
                module = load_repo_una(r["repo_dir"], r.get("una_file", "una.py"))
                if hasattr(module, "tools_configure"):
                    module.tools_configure(
                        tools_install_dir, arches=all_possible_arches
                    )
                if hasattr(module, "tools_build"):
                    module.tools_build(tools_install_dir)
                if hasattr(module, "tools_install"):
                    module.tools_install(tools_install_dir)
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
            arch_bld_dir = bld_base / arch
            staging_dir = arch_bld_dir / "staging"
            target_dir = arch_bld_dir / "target"
            runner = StepRunner(arch, staging_dir, target_dir, bld_base)

            # Ensure build directories exist and skel is propagated
            staging_dir.mkdir(parents=True, exist_ok=True)
            target_dir.mkdir(parents=True, exist_ok=True)
            if skel_dir.exists():
                colors.info(f"[{arch}] Phase -1: Skeleton Propagation (verified)")
                skel_runner = StepRunner(arch, staging_dir, target_dir, bld_base)
                skel_runner.run_step(
                    cfg={"name": "skel"},
                    step_name="propagate",
                    step_func=propagate_skel,
                )
            else:
                colors.warn(f"[{arch}] No skel directory found - empty staging/target")

            # Clean ALL target repos before build to avoid stale configs/artifacts between arches
            # This is critical for the kernel which relies on its .config in the source tree
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
                import subprocess

                if is_repo_dirty(r_path):
                    colors.error(
                        f"[{arch}] ERROR: Repository {r['name']} is dirty. Please commit or stash changes before building."
                    )
                    sys.exit(1)
                if is_enabled():
                    from mods.trace import repo_cleaned
                    repo_cleaned(Path(r_path))
                subprocess.run(["git", "clean", "-qfdx"], cwd=r_path, check=True)
                if (r_path / ".gitmodules").exists():
                    try:
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
    if tag or args.rebase:
        action = "save" if tag else "rebase"
        if tag or args.rebase == "ALL" or args.rebase == "una":
            handle_top_level_repo(BASE_DIR, action, tag, squash=True)

        repos_for_op = [
            r
            for r in repos_to_process
            if tag or args.rebase == "ALL" or args.rebase == r["name"]
        ]
        handle_repos(repos_for_op, action, tag, include_all=False)

    if args.checkout:
        handle_top_level_repo(BASE_DIR, "checkout", args.checkout, squash=True)
        handle_repos(repos_to_process, "checkout", args.checkout)

    if args.clean:
        print("\n=== Global Cleanup ===")
        import subprocess

        # 1. Clean build directory (staging and target, but keep reports?)
        # User said "removes all files produced by the build".
        # Reports are also produced by the build but useful for next build cleanup.
        # Let's keep 'report' directory but clean staging/target.
        for arch in arches:
            arch_bld_dir = bld_base / arch
            staging_dir = arch_bld_dir / "staging"
            target_dir = arch_bld_dir / "target"

            if staging_dir.exists():
                print(f"Cleaning {staging_dir}...")
                shutil.rmtree(staging_dir)
                staging_dir.mkdir(parents=True, exist_ok=True)
            if target_dir.exists():
                print(f"Cleaning {target_dir}...")
                shutil.rmtree(target_dir)
                target_dir.mkdir(parents=True, exist_ok=True)

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
        subprocess.run(
            ["git", "clean", "-xfd", "-e", "bld/", "-e", "repo/", "-q"], cwd=BASE_DIR
        )

    if args.report:
        from git import Repo
        import subprocess

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
