#!/usr/bin/env python

from unamods.utils import init_or_reset_repo

repo = init_or_reset_repo(
    repo_dir="./src/my-kernel",
    repo_url="https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux-stable",
)
