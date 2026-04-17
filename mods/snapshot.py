import os
import hashlib
import json
from pathlib import Path

def get_file_hash(path: Path) -> str:
    if path.is_symlink():
        return hashlib.sha256(str(os.readlink(path)).encode()).hexdigest()
    
    sha256_hash = hashlib.sha256()
    with open(path, "rb") as f:
        # Read in chunks to avoid memory issues with large files
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def take_snapshot(directory: Path):
    snapshot = {}
    if not directory.exists():
        return snapshot
    
    for root, dirs, files in os.walk(directory):
        root_path = Path(root)
        for name in files + dirs:
            full_path = root_path / name
            rel_path = full_path.relative_to(directory)
            
            # Skip directories in the final snapshot list if they are just containers
            # actually we might need them to detect type changes, but the user said 
            # "not to bother with directories" for cleanup.
            # However, we should record them to know what's there.
            
            is_dir = full_path.is_dir() and not full_path.is_symlink()
            if is_dir:
                ftype = 'd'
                fhash = '' # No hash for directories
            elif full_path.is_symlink():
                ftype = 'l'
                fhash = get_file_hash(full_path)
            else:
                ftype = 'f'
                fhash = get_file_hash(full_path)
            
            stat = full_path.lstat()
            snapshot[str(rel_path)] = {
                "type": ftype,
                "perm": f"{stat.st_mode & 0o777:o}",
                "hash": fhash
            }
    return snapshot

def save_snapshot(snapshot, file_path: Path):
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "w") as f:
        json.dump(snapshot, f, indent=2)

def load_snapshot(file_path: Path):
    if not file_path.exists():
        return {}
    with open(file_path, "r") as f:
        return json.load(f)

def compare_snapshots(prev, curr):
    """
    Compares prev against curr.
    Returns:
    - added: entries in curr but not in prev
    - modified: entries in both but different (hash/perm/type)
    - deleted: entries in prev but not in curr
    """
    added = {}
    modified = {}
    deleted = {}
    
    for path, meta in curr.items():
        if path not in prev:
            added[path] = meta
        else:
            p_meta = prev[path]
            if meta["hash"] != p_meta["hash"] or meta["perm"] != p_meta["perm"] or meta["type"] != p_meta["type"]:
                modified[path] = {"old": p_meta, "new": meta}
    
    for path, meta in prev.items():
        if path not in curr:
            deleted[path] = meta
            
    return added, modified, deleted

def write_report(added, modified, deleted, report_path: Path):
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w") as f:
        # CPIO-like format: type name permissions
        # We also want to include the hash for our own tracking in the cleanup step if needed,
        # but the user asked for: file type, file name, permissions.
        # "If the hash changes, that should also be treated as a difference and reported."
        
        # We'll use a clear format.
        for path, meta in added.items():
            f.write(f"A {meta['type']} {path} {meta['perm']}\n")
        
        for path, diff in modified.items():
            old = diff["old"]
            new = diff["new"]
            m_type = "M"
            if old["hash"] != new["hash"]:
                m_type = "C" # Changed content
            if old["perm"] != new["perm"]:
                m_type = "P" # Changed permissions
            if old["type"] != new["type"]:
                m_type = "T" # Changed type
                
            f.write(f"{m_type} {new['type']} {path} {new['perm']} (was {old['type']} {old['perm']})\n")
            
        for path, meta in deleted.items():
            f.write(f"D {meta['type']} {path} {meta['perm']}\n")

def get_report_paths(report_path: Path):
    """
    Returns only paths that were added or modified (and thus should be cleaned up).
    We skip directories.
    """
    paths = []
    if not report_path.exists():
        return paths
    
    with open(report_path, "r") as f:
        for line in f:
            parts = line.strip().split(" ")
            if not parts: continue
            status = parts[0]
            if status in ["A", "C", "M", "P", "T"]:
                ftype = parts[1]
                path = parts[2]
                if ftype != 'd':
                    paths.append(path)
    return paths
