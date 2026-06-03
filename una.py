#!/usr/bin/python

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from mods import colors
from mods.build import (
    load_repo_una,
    StepRunner,
    propagate_skel,
    init_build,
    run_build,
)
from mods.config import (
    set_base_dir,
    load_repo_config,
    scan_repos,
    save_repo_state,
    deduplicate_repos,
    filter_by_requested,
    list_repos,
    ConfigError,
)
from mods.deps import (
    get_build_order,
    get_keep_dirs,
    get_sync_set,
)
from mods.emulation import get_qemu_command, add_test_disk, get_console_args, run_qemu
from mods.git_ops import (
    sync_repo,
    handle_repos,
    handle_top_level_repo,
    print_top_level_status,
    get_git_remote_base,
    remove_repo,
)
from mods.packaging import create_test_disk, run_kernel
from mods.trace import (
    init_trace,
    is_enabled,
    trace_deps,
)
from mods.utils import (
    get_target_triple,
    get_arch_flags,
    get_all_arches,
    is_repo_dirty,
)

skel_dir = BASE_DIR / "skel"

try:
    import curses
    CURSES_OK = True
except ImportError:
    CURSES_OK = False


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

    try:
        from git import Repo
        top_repo = Repo(BASE_DIR)
        remote_names = [r.name for r in top_repo.remotes]
        if "una" not in remote_names or "origin" not in remote_names:
            colors.error("Error: The top-level una repository must have both 'una' and 'origin' remotes configured.")
            sys.exit(1)
    except Exception as e:
        colors.error(f"Error checking top-level repository remotes: {e}")
        sys.exit(1)

    if args.trace:
        init_trace(args.trace)

    if not args.conf:
        if args.status or args.create_disk:
            pass
        else:
            colors.error("Error: --conf is required.")
            sys.exit(1)

    base = BASE_DIR / "bld"
    tmp = base / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)

    keep_list = {'PATH', 'HOME', 'SSH_AUTH_SOCK', 'TERM'}

    global_env = {k: os.environ[k] for k in keep_list if k in os.environ}
    global_env["TMPDIR"] = str(tmp)

    os.environ.clear()
    os.environ.update(global_env)

    conf_files = [c.strip() for c in args.conf.split(",")] if args.conf else []

    if args.run and len(conf_files) > 1:
        colors.error("Error: --run requires exactly one configuration file.")
        sys.exit(1)

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

    repos_config = deduplicate_repos(repos_config)
    for cfg in repos_config:
        cfg["is_virtual"] = cfg.get("type") == "virtual"

    una_base = get_git_remote_base("una")
    origin_base = get_git_remote_base("origin")
    repos = []

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
        if "components" in global_cfg:
            required_names = {
                c.strip()
                for c in global_cfg["components"].replace(",", " ").split()
                if c.strip()
            }
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
    sync_set = get_sync_set(dep_graph, repos_config)
    repos_to_sync = set(sync_set)

    repos = [r for r in repos_config if not r.get("is_virtual") and r["name"] in repos_to_sync]

    for r in repos:
        if una_base:
            base = una_base
            if not base.endswith("/") and not base.endswith(":"):
                base += "/"
            r["una_url"] = f"{base}{r.get('una_repo', '')}"
        else:
            r["una_url"] = "UNKNOWN_BASE"

        if "origin_url" in r:
            ourl = r["origin_url"]
            if "/" not in ourl and ":" not in ourl:
                if origin_base:
                    obase = origin_base
                    if not obase.endswith("/") and not obase.endswith(":"):
                        obase += "/"
                    r["origin_url"] = f"{obase}{ourl}"
                else:
                    r["origin_url"] = f"UNKNOWN_ORIGIN_BASE/{ourl}"

    git_logs_dir = bld_base / "git_logs"
    git_logs_dir.mkdir(parents=True, exist_ok=True)

    for cfg in repos_config:
        if cfg.get("is_virtual") or cfg["name"] not in repos_to_sync:
            continue
        if "repo_dir" not in cfg:
            continue

        git_log_file = git_logs_dir / f"{cfg['name']}_git_pre.txt"
        original_stdout_fd = os.dup(1)
        original_stderr_fd = os.dup(2)
        try:
            colors.info(f">>> Syncing repo: {cfg['name']} (output -> {git_log_file})")
            with open(git_log_file, "a") as f:
                os.dup2(f.fileno(), 1)
                os.dup2(f.fileno(), 2)
                if sync_repo(cfg, una_base):
                    save_repo_state(cfg)
        finally:
            os.dup2(original_stdout_fd, 1)
            os.dup2(original_stderr_fd, 2)
            os.close(original_stdout_fd)
            os.close(original_stderr_fd)

    valid_repo_dirs = keep_repo_dirs.copy()
    for cfg in repos_config:
        if cfg.get("is_virtual") or "repo_dir" not in cfg:
            continue
        if cfg["name"] in repos_to_sync:
            valid_repo_dirs.add(Path(cfg["repo_dir"]).absolute())

    scanned = scan_repos()
    for s_cfg in scanned:
        if Path(s_cfg["repo_dir"]).absolute() not in valid_repo_dirs:
            colors.warn(
                f"Repository '{s_cfg['name']}' found in repo/ but not required by config. Removing..."
            )
            remove_repo(s_cfg["name"], repos_config, arches, bld_base)

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
        try:
            stderr_msg = (
                f"DEBUG: no_curses={args.no_curses}, stdout_isatty={sys.stdout.isatty()}, TERM={os.environ.get('TERM')}, arches={arches}\n"
            )
            sys.stderr.write(stderr_msg)
            sys.stderr.flush()
        except Exception:
            pass

        use_curses = not args.no_curses and sys.stdout.isatty()
        build_result = False

        if use_curses:
            try:
                from mods.curses_ui import CursesUI

                conf_name = Path(conf_files[0]).stem
                log_dir = str(BASE_DIR / "bld" / conf_name / "build_logs")

                try:
                    sys.stderr.write(f"DEBUG: Starting CursesUI with log_dir={log_dir}\n")
                    sys.stderr.flush()
                except Exception:
                    pass

                ui = CursesUI(log_dir=log_dir)
                init_build(
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
                build_result = ui.start(run_build, args)

                if args.run:
                    if not build_result:
                        colors.error("Build failed. Skipping --run.")
                        sys.exit(1)
                else:
                    return
            except ImportError:
                colors.error("Error: curses not available")
                sys.exit(1)
            except Exception as e:
                colors.warn(
                    f"Warning: curses UI failed ({e}), falling back to non-curses mode"
                )
                use_curses = False
                try:
                    import curses
                    curses.echo()
                    curses.nocbreak()
                    curses.endwin()
                except Exception:
                    pass
                try:
                    subprocess.run(['stty', 'sane'], check=False)
                except Exception:
                    pass

        init_build(
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

        if args.run:
            if not build_result:
                colors.error("Build failed. Skipping --run.")
                sys.exit(1)
        else:
            return

    if args.run:
        run_kernel(repos, arches, global_cfg, bld_base)

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

        tools_install_dir = BASE_DIR / "bld" / "tools"
        if tools_install_dir.exists():
            print(f"Cleaning {tools_install_dir}...")
            shutil.rmtree(tools_install_dir)
            tools_install_dir.mkdir(parents=True, exist_ok=True)

        cleaned_dirs = set()
        for r in repos:
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
                subprocess.run(["git", "clean", "-fdx", "-q"], cwd=r_path, check=True, stdin=subprocess.DEVNULL)
                cleaned_dirs.add(r_path)

        repo_base = BASE_DIR / "repo"
        if repo_base.exists():
            valid_repo_dirs_clean = {
                Path(r["repo_dir"]).absolute()
                for r in repos
                if not r.get("is_virtual") and "repo_dir" in r
            }
            for d in repo_base.iterdir():
                if d.is_dir() and d.absolute() not in valid_repo_dirs_clean:
                    print(f"Removing unreferenced directory {d}...")
                    shutil.rmtree(d, ignore_errors=True)

        print("Cleaning top-level workspace...")
        if is_repo_dirty(BASE_DIR):
            print("ERROR: Top-level repository is dirty. Stopping global cleanup.")
            sys.exit(1)
        print(f">>> git clean -xfd -e bld/ -e repo/ -q  (in {BASE_DIR})")
        subprocess.run(
            ["git", "clean", "-xfd", "-e", "bld/", "-e", "repo/", "-q"], cwd=BASE_DIR,
            stdin=subprocess.DEVNULL,
        )

    if args.report:
        from git import Repo

        print("\n=== Generating Change Report ===")
        report_dir = bld_base / "report"
        report_dir.mkdir(parents=True, exist_ok=True)

        summary_file = report_dir / "summary.txt"
        summary_lines = []

        processed_dirs = set()

        all_repos_to_report = repos

        for cfg in all_repos_to_report:
            r_path = Path(cfg["repo_dir"]).absolute()
            if r_path in processed_dirs:
                continue
            if not r_path.exists() or not (r_path / ".git").exists():
                continue

            repo = Repo(r_path)
            name = cfg["name"]

            remote_prefix = "origin" if "origin_url" in cfg else "una"
            branch = cfg.get("branch")
            tag_name = cfg.get("tag")
            if branch:
                target = f"{remote_prefix}/{branch}"
            elif tag_name:
                target = tag_name
            else:
                from mods.utils import get_remote_head
                target = f"{remote_prefix}/{get_remote_head(repo, remote_prefix)}"

            print(f"[{name}] Comparing against {target}...")

            try:
                diffstat = repo.git.diff(target, "--stat")
                full_diff = repo.git.diff(target)

                if not diffstat.strip():
                    diffstat = "No changes."
                    full_diff = ""

                diff_filename = f"{name}.diff"
                diff_file = report_dir / diff_filename
                diff_file.write_text(full_diff)

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
