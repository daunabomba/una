import os
import stat
from pathlib import Path

def generate_list(root_dir: str):
    """
    Python implementation of the filelist generation script.
    A single traversal pass to generate directory, file, and symlink entries.
    """
    root_path = Path(root_dir).resolve()
    
    # 1. Traverse everything
    for root, dirs, files in os.walk(root_path):
        current_dir = Path(root)
        rel_root = current_dir.relative_to(root_path)
        
        # Current directory itself (except the absolute root)
        if str(rel_root) != ".":
            mode = oct(current_dir.stat().st_mode & 0o7777)[2:]
            print(f"dir /{rel_root} {mode} 0 0")
        
        # Files and symlinks in this directory
        for item in files:
            full_path = current_dir / item
            rel_path = full_path.relative_to(root_path)
            
            # Check if it's a symlink first
            if full_path.is_symlink():
                mode = oct(full_path.lstat().st_mode & 0o7777)[2:]
                link_target = os.readlink(full_path)
                print(f"slink /{rel_path} {link_target} {mode} 0 0")
            else:
                mode = oct(full_path.stat().st_mode & 0o7777)[2:]
                print(f"file /{rel_path} {full_path} {mode} 0 0")

    # 4. Hardcoded device node 
    print("nod /dev/console 600 0 0 c 5 1")
