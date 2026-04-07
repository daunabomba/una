#!/usr/bin/python

import argparse
import shutil
import os

from mods.utils import init_or_reset_repo
from pathlib import Path

bld_base = Path("./bld").absolute()

host_install_dir = bld_base / "host"
staging_dir = bld_base / "staging"
target_dir = bld_base / "target"

import importlib.util
import sys


def load_repo_una(repo_dir: str):
    """
    Dynamically load the una.py file from the specified repo directory.
    """
    una_file = Path(repo_dir) / "una.py"
    if not una_file.exists():
        print(f"Warning: {una_file} not found. Skipping build for this repo.")
        return None
    
    module_name = f"repo_una_{Path(repo_dir).name}"
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
        print(f"[{r.get('type', 'unknown')}] {r['repo_dir']}")
    return [r["repo_dir"] for r in filtered]


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
        action="store_true",
        help="Initialize or reinit repos.",
    )
    parser.add_argument(
        "--list",
        choices=["host", "target", "all"],
        help="List repos of the specified type.",
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="Build it.",
    )

    args = parser.parse_args()

    repos = [
        {
            "repo_dir": "./repo/llvm",
            "origin_url": "/mnt/work/bld/llvm-project.git",
            "una_url": "git@github.com:daunabomba/llvm-project.git",
            "type": "host",
        },
        {
            "repo_dir": "./repo/musl",
            "origin_url": "https://git.musl-libc.org/git/musl",
            "una_url": "git@github.com:daunabomba/musl.git",
            "type": "target",
        },
        {
            "repo_dir": "./repo/busybox",
            "origin_url": "https://git.busybox.net/busybox",
            "una_url": "git@github.com:daunabomba/busybox.git",
            "type": "target",
        },
        {
            "repo_dir": "./repo/kernel",
            "origin_url": "/mnt/work/bld/linux-stable.git",
            "una_url": "git@github.com:daunabomba/linux.git",
            "type": "target",
        },
    ]

    if args.list:
        target_type = None if args.list == "all" else args.list
        list_repos(repos, target_type)

    if args.init:
        shutil.rmtree(bld_base, ignore_errors=True)
        host_install_dir.mkdir(parents=True, exist_ok=True)
        staging_dir.mkdir(parents=True, exist_ok=True)
        target_dir.mkdir(parents=True, exist_ok=True)
        for cfg in repos:
            repo_dir = cfg["repo_dir"]
            origin_url = cfg["origin_url"]
            una_url = cfg["una_url"]

            if args.dry_run:
                print(f"[DRY RUN] Would init/reset repo at {repo_dir} from {origin_url}")
            else:
                print(f"Initializing or resetting repo at {repo_dir}...")
                repo = init_or_reset_repo(repo_dir=repo_dir, origin_url=origin_url, una_url=una_url)
                print(f"Done with repo: {repo.working_dir}\n")

    if args.build:
        print("Starting host build.")
        host_repos = [r for r in repos if r["type"] == "host"]
        for r in host_repos:
            repo_dir = r["repo_dir"]
            module = load_repo_una(repo_dir)
            if module and hasattr(module, "host_configure"):
                module.host_configure(host_install_dir)
                module.host_build(host_install_dir)
                module.host_install(host_install_dir)

        print("\nStarting target build.")
        target_repos = [r for r in repos if r["type"] == "target"]
        
        # Create compiler config file for target builds
        musl_cfg = bld_base / "muslx32.cfg"
        print(f"Creating compiler configuration at {musl_cfg}...")
        cfg_content = f"""--target=x86_64-linux-muslx32
--sysroot={staging_dir}
-fuse-ld=lld
-nostdlib
{staging_dir}/usr/lib/Scrt1.o
{staging_dir}/usr/lib/crti.o
-L{staging_dir}/usr/lib
-lc
{staging_dir}/usr/lib/crtn.o
-fPIE
"""
        musl_cfg.write_text(cfg_content)
        
        # Set environment variable for child processes
        os.environ["CFLAGS"] = f"--config={musl_cfg} -pipe -D_FILE_OFFSET_BITS=64"

        # Phase 1: Configure and Install headers for all targets
        print("Phase 1: Configuring and installing target headers.")
        for r in target_repos:
            repo_dir = r["repo_dir"]
            module = load_repo_una(repo_dir)
            if module:
                if hasattr(module, "target_configure"):
                    module.target_configure(staging_dir, target_dir)
                if hasattr(module, "target_headers_install"):
                    module.target_headers_install(staging_dir, target_dir)
            
        # Phase 2: Build and install for all targets
        print("Phase 2: Building and installing target packages.")
        for r in target_repos:
            repo_dir = r["repo_dir"]
            module = load_repo_una(repo_dir)
            if module:
                if hasattr(module, "target_build"):
                    module.target_build(staging_dir, target_dir)
                if hasattr(module, "target_install"):
                    module.target_install(staging_dir, target_dir)
    elif not args.list:
        print("No action specified. Use --init to initialize repos or --list to see them --build to build after calling --init.")

if __name__ == "__main__":
    main()
