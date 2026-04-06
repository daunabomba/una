#!/usr/bin/python

import argparse
from mods.utils import init_or_reset_repo


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
    args = parser.parse_args()

    repos = [
        {
            "repo_dir": "./bld/llvm",
            "origin_url": "/mnt/work/bld/llvm-project.git",
            "una_url": "git@github.com:daunabomba/llvm-project.git",
            "type": "host",
        },
        {
            "repo_dir": "./src/kernel",
            "origin_url": "/mnt/work/bld/linux-stable.git",
            "una_url": "git@github.com:daunabomba/linux.git",
            "type": "target",
        },
        {
            "repo_dir": "./src/musl",
            "origin_url": "https://git.musl-libc.org/git/musl",
            "una_url": "git@github.com:daunabomba/musl.git",
            "type": "target",
        },
        {
            "repo_dir": "./src/busybox",
            "origin_url": "https://git.busybox.net/busybox",
            "una_url": "git@github.com:daunabomba/busybox.git",
            "type": "target",
        },
    ]

    if args.list:
        target_type = None if args.list == "all" else args.list
        list_repos(repos, target_type)

    if args.init:
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
    elif not args.list:
        print("No action specified. Use --init to initialize repos or --list to see them.")


if __name__ == "__main__":
    main()
