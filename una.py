#!/usr/bin/python

import argparse
from mods.utils import init_or_reset_repo


def parse_args():
    parser = argparse.ArgumentParser(
        description="Initialize or reset Git repos from a list.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without cloning/resetting.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Run git clean -fdx on each repo.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    repos = [
        {
            "repo_dir": "./bld/llvm",
            "origin_url": "https://github.com/llvm/llvm-project.git",
            "una_url": "git@github.com:daunabomba/llvm-project.git",
        },
        {
            "repo_dir": "./src/kernel",
            "origin_url": "https://kernel.googlesource.com/pub/scm/linux/kernel/git/stable/linux-stable.git",
            "una_url": "git@github.com:daunabomba/linux.git",
        },
        {
            "repo_dir": "./src/musl",
            "origin_url": "https://git.musl-libc.org/git/musl",
            "una_url": "git@github.com:daunabomba/musl.git",
        },
        {
            "repo_dir": "./src/busybox",
            "origin_url": "https://git.busybox.net/busybox",
            "una_url": "git@github.com:daunabomba/busybox.git",
        },
    ]

    for cfg in repos:
        repo_dir = cfg["repo_dir"]
        origin_url = cfg["origin_url"]
        una_url = cfg["una_url"]

        if args.dry_run:
            print(f"[DRY RUN] Would init/reset repo at {repo_dir} from {origin_url}")
            if args.clean:
                print(f"[DRY RUN] Would run git clean -fdx on {repo_dir}")
        else:
            print(f"Initializing or resetting repo at {repo_dir}...")
            repo = init_or_reset_repo(repo_dir=repo_dir, origin_url=origin_url, una_url=una_url)

            if not args.clean:
                print("Skipping git clean -fdx...")
                # Skip clean entirely; git_utils already does it by default
                # If you want to cut that out, move git clean into this main and gate it
            else:
                print("Skipping extra git clean; already done by init_or_reset_repo.")

            print(f"Done with repo: {repo.working_dir}\n")


if __name__ == "__main__":
    main()
