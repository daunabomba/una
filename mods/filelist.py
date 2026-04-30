import os
import stat
import sys
import re
from pathlib import Path


def generate_list(root_dir: str, output_file: str = None, ignore_patterns: list = None):
    """
    Python implementation of the filelist generation script.
    Generates a list of directories, files, and symlinks in cpio-list format.

    :param ignore_patterns: List of regex patterns to ignore (checked against relative path)
    """
    root_path = Path(root_dir).resolve()

    # Pre-compile regexes
    regexes = [re.compile(p) for p in ignore_patterns] if ignore_patterns else []

    out = open(output_file, "w") if output_file else sys.stdout

    try:

        def write(msg):
            out.write(msg + "\n")

        def should_ignore(rel_path_str: str):
            for regex in regexes:
                if regex.search(rel_path_str):
                    return True
            return False

        # 1. Traverse everything
        for root, dirs, files in os.walk(root_path):
            current_dir = Path(root)

            # Filter directories to prevent descending into ignored ones
            # We modify dirs in-place for os.walk
            to_remove = []
            for d in dirs:
                full_path = current_dir / d
                rel_path = full_path.relative_to(root_path)
                if should_ignore(str(rel_path)):
                    to_remove.append(d)
                else:
                    # Not ignored, check if it's a symlink
                    if full_path.is_symlink():
                        mode = oct(full_path.lstat().st_mode & 0o7777)[2:]
                        link_target = os.readlink(full_path)
                        write(f"slink /{rel_path} {link_target} {mode} 0 0")
                        # We should also prevent os.walk from descending
                        # into this if it was a symlink to a dir
                        to_remove.append(d)
                    else:
                        mode = oct(full_path.stat().st_mode & 0o7777)[2:]
                        write(f"dir /{rel_path} {mode} 0 0")

            for d in to_remove:
                dirs.remove(d)

            for f in files:
                full_path = current_dir / f
                rel_path = full_path.relative_to(root_path)

                if should_ignore(str(rel_path)):
                    continue

                if full_path.is_symlink():
                    mode = oct(full_path.lstat().st_mode & 0o7777)[2:]
                    link_target = os.readlink(full_path)
                    write(f"slink /{rel_path} {link_target} {mode} 0 0")
                else:
                    mode = oct(full_path.stat().st_mode & 0o7777)[2:]
                    write(f"file /{rel_path} {full_path} {mode} 0 0")

        # 4. Hardcoded device node
        write("nod /dev/console 600 0 0 c 5 1")
    finally:
        if output_file:
            out.close()
