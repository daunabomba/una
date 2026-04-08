import os
import stat
import sys
from pathlib import Path

def generate_list(root_dir: str, output_file: str = None):
    """
    Python implementation of the filelist generation script.
    Generates a list of directories, files, and symlinks in cpio-list format.
    """
    root_path = Path(root_dir).resolve()
    
    out = open(output_file, "w") if output_file else sys.stdout
    
    try:
        def write(msg):
            out.write(msg + "\n")

        # 1. Traverse everything
        for root, dirs, files in os.walk(root_path):
            current_dir = Path(root)
            
            # Handle entries in this directory
            # We check both 'dirs' and 'files' because symlinks to directories
            # appear in 'dirs' when followlinks=False.
            
            for d in dirs:
                full_path = current_dir / d
                rel_path = full_path.relative_to(root_path)
                
                # Check if it's a symlink
                if full_path.is_symlink():
                    mode = oct(full_path.lstat().st_mode & 0o7777)[2:]
                    link_target = os.readlink(full_path)
                    write(f"slink /{rel_path} {link_target} {mode} 0 0")
                else:
                    mode = oct(full_path.stat().st_mode & 0o7777)[2:]
                    write(f"dir /{rel_path} {mode} 0 0")
            
            for f in files:
                full_path = current_dir / f
                rel_path = full_path.relative_to(root_path)
                
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
