#!/usr/bin/python

import argparse
import shutil
import os

from mods.utils import init_or_reset_repo, rebase_and_push
from pathlib import Path

bld_base = Path("./bld").absolute()

host_install_dir = bld_base / "host"
staging_dir = bld_base / "staging"
target_dir = bld_base / "target"

import importlib.util
import sys


def load_repo_una(repo_dir: str, una_file_name: str = "una.py"):
    """
    Dynamically load the specified una file from the repo directory.
    Defaults to 'una.py'.
    """
    una_file = Path(repo_dir) / una_file_name
    if not una_file.exists():
        print(f"Error: {una_file} not found. Build script is missing for this component.")
        sys.exit(1)
    
    # Create a unique module name based on repo name and script name
    unique_id = f"{Path(repo_dir).name}_{una_file_name.replace('/', '_').replace('.', '_')}"
    module_name = f"repo_una_{unique_id}"
    
    spec = importlib.util.spec_from_file_location(module_name, una_file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def list_repos(repos, target_type=None):
    """
    Helper function to filter and print repo directories by type.
    If target_type is None, prints all.
    """
    filtered = [r for r in repos if target_type is None or r.get("type") == target_type]
    for r in filtered:
        script_info = f" (Script: {r.get('una_file', 'una.py')})"
        print(f"[{r.get('type', 'unknown')}] {r['name']} -> {r['repo_dir']}{script_info}")
    return [r["name"] for r in filtered]


def main():
    parser = argparse.ArgumentParser(
        description="Initialize or reset Git repos from a list.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without cloning/resetting.",
    )
    parser.add_argument(
        "--init",
        metavar="BASE_URL",
        help="Initialize or reinit repos with the specified 'una' base URL.",
    )
    parser.add_argument(
        "--list",
        choices=["host", "target", "all"],
        help="List repos of the specified type.",
    )
    parser.add_argument(
        "--build",
        nargs="?",
        const="ALL",
        help="Build all projects (if no argument) or a specific component by name.",
    )
    parser.add_argument(
        "--rebase",
        action="store_true",
        help="Rebase the local 'una' branch onto its configured upstream origin branch and push to una.",
    )

    args = parser.parse_args()

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    # Configuration for components and repositories
    repos_config = [
        {
            "name": "llvm-host",
            "una_repo": "llvm-project.git",
            "repo_dir": "./repo/llvm",
            "una_file": "una/host.py",
            "origin_url": "/mnt/work/bld/llvm-project.git",
            "type": "host",
            "branch": "main",
        },
        {
            "name": "musl",
            "una_repo": "musl.git",
            "repo_dir": "./repo/musl",
            "origin_url": "https://git.musl-libc.org/git/musl",
            "type": "target",
            "branch": "master",
        },
        {
            "name": "llvm-runtime",
            "una_repo": "llvm-project.git",
            "repo_dir": "./repo/llvm",
            "una_file": "una/runtime.py",
            "origin_url": "/mnt/work/bld/llvm-project.git",
            "type": "target",
            "branch": "main",
        },
        {
            "name": "busybox",
            "una_repo": "busybox.git",
            "repo_dir": "./repo/busybox",
            "origin_url": "https://git.busybox.net/busybox",
            "type": "target",
            "branch": "master",
        },
        {
            "name": "linux",
            "una_repo": "linux.git",
            "repo_dir": "./repo/kernel",
            "origin_url": "/mnt/work/bld/linux-stable.git",
            "type": "target",
            "branch": "master",
        },
    ]

    repos = []
    una_base = args.init
    
    for r in repos_config:
        config = r.copy()
        if una_base:
            base = una_base
            if not base.endswith("/") and not base.endswith(":"):
                base += "/"
            config["una_url"] = f"{base}{r['una_repo']}"
        else:
            config["una_url"] = "UNKNOWN_BASE" 
        repos.append(config)

    # Filtered view for operation execution
    if args.build and args.build != "ALL":
        target_name = args.build
        repos_to_process = [r for r in repos if r["name"] == target_name]
        if not repos_to_process:
            print(f"Error: Component '{target_name}' not found.")
            sys.exit(1)
    else:
        repos_to_process = repos

    # Check if repos exist before building or rebasing (unless we are also initializing them)
    if (args.build or args.rebase) and not args.init:
        missing = [r["name"] for r in repos_to_process if not Path(r["repo_dir"]).exists()]
        if missing:
            print(f"Warning: The following repository directories are missing: {', '.join(missing)}")
            print("Please run with --init [BASE_URL] first to initialize the repositories.")
            sys.exit(1)

    if args.list:
        target_type = None if args.list == "all" else args.list
        list_repos(repos, target_type)
        return

    if args.init:
        # Full wipe only on non-specific init
        if args.build == "ALL" or not args.build:
            shutil.rmtree(bld_base, ignore_errors=True)
            host_install_dir.mkdir(parents=True, exist_ok=True)
            staging_dir.mkdir(parents=True, exist_ok=True)
            target_dir.mkdir(parents=True, exist_ok=True)

            skel_dir = Path("skel")
            if skel_dir.exists():
                print(f"Propagating skel contents to staging and target directories...")
                shutil.copytree(skel_dir, staging_dir, symlinks=True, dirs_exist_ok=True)
                shutil.copytree(skel_dir, target_dir, symlinks=True, dirs_exist_ok=True)
            
        initialized_dirs = set()
        for cfg in repos_to_process:
            repo_dir = cfg["repo_dir"]
            if repo_dir in initialized_dirs:
                continue
            init_or_reset_repo(repo_dir=repo_dir, origin_url=cfg["origin_url"], una_url=cfg["una_url"])
            initialized_dirs.add(repo_dir)

    if args.build:
        print("Starting host build.")
        host_repos = [r for r in repos_to_process if r["type"] == "host"]
        for r in host_repos:
            module = load_repo_una(r["repo_dir"], r.get("una_file", "una.py"))
            if hasattr(module, "host_configure"): module.host_configure(host_install_dir)
            if hasattr(module, "host_build"): module.host_build(host_install_dir)
            if hasattr(module, "host_install"): module.host_install(host_install_dir)

        print("\nStarting target build.")
        target_repos = [r for r in repos_to_process if r["type"] == "target"]
        all_targets = [r for r in repos if r["type"] == "target"]

        if target_repos:
            musl_cfg = bld_base / "muslx32.cfg"
            if not musl_cfg.exists():
                print(f"Creating compiler configuration at {musl_cfg}...")
                cfg_content = f"""--target=x86_64-linux-muslx32
--sysroot={staging_dir}
-fuse-ld=lld
-nostdlib
-L{staging_dir}/usr/lib
-lc
-fPIE
-mx32
"""
                musl_cfg.write_text(cfg_content)
            os.environ["CFLAGS"] = f"--config={musl_cfg} -pipe -D_FILE_OFFSET_BITS=64"

            # Phase 0: Core Setup (Always check for basic headers)
            print("Phase 0: Core Headers (musl & linux).")
            for name in ["musl", "linux"]:
                proj = next((r for r in all_targets if r["name"] == name), None)
                if proj:
                    module = load_repo_una(proj["repo_dir"], proj.get("una_file", "una.py"))
                    if hasattr(module, "target_configure"): module.target_configure(staging_dir, target_dir)
                    if hasattr(module, "target_headers_install"): module.target_headers_install(staging_dir, target_dir)

            # Phase 1: Build Musl (required for project configuration checks)
            print("Phase 1: Building C Library (musl).")
            musl_proj = next((r for r in all_targets if r["name"] == "musl"), None)
            if musl_proj:
                module = load_repo_una(musl_proj["repo_dir"], musl_proj.get("una_file", "una.py"))
                if hasattr(module, "target_build"): module.target_build(staging_dir, target_dir)
                if hasattr(module, "target_install"): module.target_install(staging_dir, target_dir)

            # Phase 2: Configure the rest
            print("Phase 2: Configuring projects.")
            for r in target_repos:
                if r["name"] == "musl": continue
                module = load_repo_una(r["repo_dir"], r.get("una_file", "una.py"))
                if hasattr(module, "target_configure"): module.target_configure(staging_dir, target_dir)
                if hasattr(module, "target_headers_install"): module.target_headers_install(staging_dir, target_dir)

            # Phase 3: Build & Install the rest
            print("Phase 3: Final builds.")
            for r in target_repos:
                if r["name"] == "musl": continue
                module = load_repo_una(r["repo_dir"], r.get("una_file", "una.py"))
                if hasattr(module, "target_build"): module.target_build(staging_dir, target_dir)
                if hasattr(module, "target_install"): module.target_install(staging_dir, target_dir)

    if args.rebase:
        print("Starting rebase and push process for targeted repos...")
        from git import Repo
        rebased_dirs = set()
        for cfg in repos_to_process:
            repo_dir = cfg["repo_dir"]
            if repo_dir in rebased_dirs: continue
            print(f"Processing rebase for {repo_dir}...")
            rebase_and_push(Repo(repo_dir), f"origin/{cfg.get('branch', 'master')}")
            rebased_dirs.add(repo_dir)


if __name__ == "__main__":
    main()
