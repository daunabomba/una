#!/usr/bin/python

import argparse
import shutil
import os
import sys
import configparser
import json
from pathlib import Path

# Add the script's directory to sys.path so 'mods' can be imported from anywhere
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from mods.utils import init_or_reset_repo, rebase_and_push, save_and_push, get_target_triple, get_arch_flags, TqdmProgress, get_remote_head
from mods.snapshot import take_snapshot, compare_snapshots, write_report, get_report_paths
from mods import colors

bld_base = BASE_DIR / "bld"
skel_dir = BASE_DIR / "skel"

import importlib.util


def load_repo_una(repo_dir: str, una_file_name: str = "una.py"):
    """
    Dynamically load the specified una file from the repo directory.
    Defaults to 'una.py'.
    """
    una_file = Path(repo_dir) / una_file_name
    if not una_file.exists():
        colors.error(f"Error: {una_file} not found. Build script is missing for this component.")
        sys.exit(1)
    
    # Create a unique module name based on repo name and script name
    unique_id = f"{Path(repo_dir).name}_{una_file_name.replace('/', '_').replace('.', '_')}"
    module_name = f"repo_una_{unique_id}"
    
    spec = importlib.util.spec_from_file_location(module_name, una_file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class StepRunner:
    def __init__(self, arch, staging_dir, target_dir):
        self.arch = arch
        self.staging_dir = staging_dir
        self.target_dir = target_dir
        self.component_snapshots = {} # name -> {staging: {}, target: {}}
        self.cleaned_components = set()

    def run_step(self, cfg, step_name, step_func, **kwargs):
        name = cfg["name"]
        colors.info(f"[{self.arch}] Running {name}::{step_name}...")
        
        # 1. Cleanup and Pre-snapshot on first call for this component
        if name not in self.cleaned_components:
            report_file = bld_base / self.arch / "report" / f"{name}.txt"
            if report_file.exists():
                colors.info(f"[{self.arch}] Cleaning up previous build outputs for {name}...")
                paths = get_report_paths(report_file)
                for p in paths:
                    try:
                        if p.startswith("staging/"):
                            (self.staging_dir / p[8:]).unlink(missing_ok=True)
                        elif p.startswith("target/"):
                            (self.target_dir / p[7:]).unlink(missing_ok=True)
                    except Exception as e:
                        colors.warn(f"[{self.arch}] Warning: Failed to remove {p}: {e}")
            
            self.component_snapshots[name] = {
                "staging": take_snapshot(self.staging_dir),
                "target": take_snapshot(self.target_dir)
            }
            self.cleaned_components.add(name)

        # 2. Execute step
        step_func(self.staging_dir, self.target_dir, **kwargs)

        # 3. Post-snapshot and report
        pre = self.component_snapshots[name]
        post_staging = take_snapshot(self.staging_dir)
        post_target = take_snapshot(self.target_dir)
        
        added_s, mod_s, del_s = compare_snapshots(pre["staging"], post_staging)
        added_t, mod_t, del_t = compare_snapshots(pre["target"], post_target)
        
        if mod_s or del_s:
            colors.error(f"[{self.arch}] ERROR: {name} modified or deleted files in staging!")
        if mod_t or del_t:
            colors.error(f"[{self.arch}] ERROR: {name} modified or deleted files in target!")
            
        # Compile combined report
        combined_added = {f"staging/{k}": v for k, v in added_s.items()}
        combined_added.update({f"target/{k}": v for k, v in added_t.items()})
        
        combined_mod = {f"staging/{k}": v for k, v in mod_s.items()}
        combined_mod.update({f"target/{k}": v for k, v in mod_t.items()})
        
        combined_del = {f"staging/{k}": v for k, v in del_s.items()}
        combined_del.update({f"target/{k}": v for k, v in del_t.items()})
        
        report_file = bld_base / self.arch / "report" / f"{name}.txt"
        write_report(combined_added, combined_mod, combined_del, report_file)


def is_repo_dirty(repo_path: Path):
    """
    Check if a git repository has any modified or untracked files.
    """
    import subprocess
    if not (repo_path / ".git").exists():
        return False
    result = subprocess.run(["git", "status", "--porcelain"], cwd=repo_path, capture_output=True, text=True)
    return len(result.stdout.strip()) > 0


def sync_kernel_config(src: Path, dest: Path):
    """
    Syncs back the updated kernel config, stripping leading comments
    and forcing specific values like CONFIG_CC_VERSION_TEXT.
    """
    if not src.exists():
        return
    content = src.read_text()
    lines = content.splitlines()
    out_lines = []
    
    # Skip leading comments and empty lines
    header_done = False
    for line in lines:
        if not header_done:
            if line.strip().startswith("#") or not line.strip():
                continue
            else:
                header_done = True
        
        # Process entries
        if line.startswith("CONFIG_CC_VERSION_TEXT="):
            out_lines.append('CONFIG_CC_VERSION_TEXT="clang"')
        else:
            out_lines.append(line)
            
    dest.write_text("\n".join(out_lines) + "\n")


def list_repos(repos, target_type=None):
    """
    Helper function to filter and print repo directories by type.
    If target_type is None, prints all.
    """
    if target_type == "target":
        filtered = [r for r in repos if r.get("type") != "tools"]
    else:
        filtered = [r for r in repos if target_type is None or r.get("type") == target_type]
    for r in filtered:
        script_info = f" (Script: {r.get('una_file', 'una.py')})"
        print(f"[{r.get('type', 'unknown')}] {r['name']} -> {r['repo_dir']}{script_info}")
    return [r["name"] for r in filtered]


def get_git_remote_base():
    """
    Attempts to determine the base URL of the current git repository's remote.
    Specifically looks for a remote named 'una'.
    """
    try:
        from git import Repo
        # Use the absolute path of the script's directory for more reliable repo discovery
        script_dir = Path(__file__).resolve().parent
        repo = Repo(script_dir, search_parent_directories=True)
        
        # Find the remote named 'una' explicitly
        for r in repo.remotes:
            if r.name == 'una':
                url = str(r.url)
                if "/" in url:
                    return url.rsplit("/", 1)[0]
    except Exception:
        pass
    return None


def create_test_disk(disk_path):
    if disk_path.exists():
        print(f"Test disk {disk_path} already exists. Skipping creation.")
        return
    
    print(f"Creating 1G test disk at {disk_path}...")
    import subprocess
    # 1. Create 1G raw image
    subprocess.run(["qemu-img", "create", "-f", "raw", str(disk_path), "1G"], check=True)
    
    # 2. Partition with sgdisk
    # Alignment=1 to allow sector 3. Table size reduced to 4 entries to fit starting at sector 3.
    subprocess.run(["sgdisk", "--set-alignment=1", "--resize-table=4", str(disk_path)], check=True)
    subprocess.run(["sgdisk", "--set-alignment=1", "--new=1:3:65365", str(disk_path)], check=True)
    subprocess.run(["sgdisk", "--typecode=1:ef00", str(disk_path)], check=True)
    subprocess.run(["sgdisk", "--set-alignment=1", "--new=2:65536:0", str(disk_path)], check=True)
    subprocess.run(["sgdisk", "--typecode=2:8300", str(disk_path)], check=True)
    
    # 3. Format Partitions
    p1_sectors = 65365 - 3 + 1
    p1_size = p1_sectors * 512
    
    # Calculate P2 size. 1G = 2097152 sectors.
    # We find the actual last sector from sgdisk or just assume 1G minus GPT overhead.
    total_sectors = 1024 * 1024 * 1024 // 512
    p2_sectors = total_sectors - 65536 - 34 # 34 for the backup GPT at the end
    p2_size = p2_sectors * 512
    
    p1_img = disk_path.with_suffix(".p1.tmp")
    p2_img = disk_path.with_suffix(".p2.tmp")
    
    try:
        # Format P1 (FAT16 for EFI)
        print("Formatting Partition 1 (FAT16)...")
        p1_img.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["truncate", "-s", str(p1_size), str(p1_img)], check=True)
        subprocess.run(["mkfs.fat", "-f1", "-F16", "-n", "BOOT0EFI", str(p1_img)], check=True)
        subprocess.run(["dd", f"if={p1_img}", f"of={disk_path}", "bs=512", "seek=3", "conv=notrunc"], check=True)
        
        # Format P2 (EXT4)
        print("Formatting Partition 2 (EXT4)...")
        subprocess.run(["truncate", "-s", str(p2_size), str(p2_img)], check=True)
        subprocess.run(["mkfs.ext4", "-F", str(p2_img)], check=True)
        subprocess.run(["dd", f"if={p2_img}", f"of={disk_path}", "bs=512", "seek=65536", "conv=notrunc"], check=True)
        
        print("Test disk created successfully.")
    except Exception as e:
        colors.error(f"Error creating test disk: {e}")
        if disk_path.exists(): disk_path.unlink()
        raise
    finally:
        if p1_img.exists(): p1_img.unlink()
        if p2_img.exists(): p2_img.unlink()

def propagate_skel(staging_dir, target_dir):
    """Skel propagation using original file-by-file method + snapshot verification"""
    import subprocess

    colors.info("Propagating skeleton (original method)...")

    for dest in [staging_dir, target_dir]:
        # ORIGINAL logic: Handle ONLY symlink/dir conflicts
        for item in os.listdir(skel_dir):
            s_item = skel_dir / item
            d_item = dest / item

            if d_item.exists() and s_item.is_symlink() and d_item.is_dir() and not d_item.is_symlink():
                colors.warn(f"Removing conflicting directory {d_item} to preserve skel symlink.")
                shutil.rmtree(d_item)

        # ORIGINAL cp -a --remove-destination (robust merge)
        subprocess.run([
            "cp", "-a", "--remove-destination",
            f"{skel_dir}/.", str(dest)
        ], check=True)

def save_repo_state(cfg: dict):
    """Saves the repository configuration to its directory for scanning."""
    repo_dir = Path(cfg["repo_dir"])
    if not repo_dir.exists():
        return
    
    state_file = repo_dir / ".una_config"
    # Convert Path objects to strings for JSON
    serializable = cfg.copy()
    serializable["repo_dir"] = str(cfg["repo_dir"])
    
    with open(state_file, "w") as f:
        json.dump(serializable, f, indent=4)

def load_repo_config(config_path: Path):
    cp = configparser.ConfigParser()
    if not config_path.exists():
        colors.error(f"Error: Config file {config_path} not found.")
        sys.exit(1)
        
    cp.read(config_path)
    
    global_cfg = {}
    if 'una' in cp.sections():
        global_cfg = dict(cp['una'])
        cp.remove_section('una')
    
    raw_configs = {}
    for section in cp.sections():
        raw_configs[section] = dict(cp[section])

    repo_files = []
    if 'repos' in global_cfg:
        repo_files = [r.strip() for r in global_cfg['repos'].replace('\\', ' ').split() if r.strip()]
    else:
        repo_files = [str(p.relative_to(BASE_DIR)) for p in (BASE_DIR / "confs" / "repos").glob("*.repo")]
        
    for r_file in repo_files:
        r_path = BASE_DIR / r_file
        if r_path.exists():
            rcp = configparser.ConfigParser()
            rcp.read(r_path)
            for section in rcp.sections():
                raw_configs[section] = dict(rcp[section])
        else:
            colors.warn(f"Warning: Repo config {r_path} not found.")
    
    final_repos = []
    for name in raw_configs:
        # Resolve references to get a flat dict of strings first
        visited = set()
        current_cfg = raw_configs[name].copy()
        resolving_name = name
        
        while 'ref' in current_cfg:
            ref_name = current_cfg['ref']
            if ref_name in visited:
                colors.error(f"Error: Circular reference detected for repo {name}")
                sys.exit(1)
            if ref_name not in raw_configs:
                colors.error(f"Error: Reference {ref_name} not found for {resolving_name}")
                sys.exit(1)
            
            # Combine parent into current (parent provides defaults, current overrides)
            parent_base = raw_configs[ref_name].copy()
            child_overrides = current_cfg.copy()
            del child_overrides['ref']
            
            parent_base.update(child_overrides)
            current_cfg = parent_base
            visited.add(ref_name)
        
        cfg = current_cfg
        cfg['name'] = name
        
        # Post-process types AFTER all merges are done for this repo
            
            
        if 'sparse_ignore_dirs' in cfg:
            cfg['sparse_ignore_dirs'] = [s.strip() for s in cfg['sparse_ignore_dirs'].split(',') if s.strip()]
        else:
            cfg['sparse_ignore_dirs'] = []
            
        if 'repo_dir' in cfg:
            # If relative, it's relative to BASE_DIR
            rd = Path(cfg['repo_dir'])
            if not rd.is_absolute():
                cfg['repo_dir'] = BASE_DIR / rd
            else:
                cfg['repo_dir'] = rd
        
        # Handle kernel_image map
        kimg = {}
        for key in list(cfg.keys()):
            if key.startswith('kernel_image.'):
                arch = key.split('.', 1)[1]
                kimg[arch] = cfg[key]
                del cfg[key]
        if kimg:
            cfg['kernel_image'] = kimg
            
        final_repos.append(cfg)

    for cfg in final_repos:
        if 'depends' in cfg:
            cfg['depends'] = [s.strip() for s in cfg['depends'].replace(',', ' ').split() if s.strip()]
        else:
            cfg['depends'] = []
        cfg["is_virtual"] = cfg.get("type") == "virtual"

    return final_repos, global_cfg

def scan_repos():
    """Scans the repo/ directory for existing repositories and their states."""
    repo_base = BASE_DIR / "repo"
    if not repo_base.exists():
        return []
    
    scanned = []
    for d in repo_base.iterdir():
        if d.is_dir():
            state_file = d / ".una_config"
            if state_file.exists():
                try:
                    with open(state_file, "r") as f:
                        cfg = json.load(f)
                        rd = Path(cfg["repo_dir"])
                        if not rd.is_absolute():
                            cfg["repo_dir"] = BASE_DIR / rd
                        else:
                            cfg["repo_dir"] = rd
                        scanned.append(cfg)
                except Exception as e:
                    print(f"Warning: Failed to load state for {d}: {e}")
    return scanned

def remove_repo(name, repos, arches):
    """Removes a repository from the list, cleans build outputs and deletes repo dir."""
    target = next((r for r in repos if r["name"] == name), None)
    if not target:
        colors.error(f"Error: Repository '{name}' not found.")
        return False

    colors.info(f"Removing repository '{name}'...")
    
    # 1. Clean build outputs for each architecture
    for arch in arches:
        report_file = bld_base / arch / "report" / f"{name}.txt"
        if report_file.exists():
            colors.info(f"[{arch}] Cleaning build outputs for {name}...")
            paths = get_report_paths(report_file)
            staging_dir = bld_base / arch / "staging"
            target_dir = bld_base / arch / "target"
            
            for p in paths:
                try:
                    if p.startswith("staging/"):
                        (staging_dir / p[8:]).unlink(missing_ok=True)
                    elif p.startswith("target/"):
                        (target_dir / p[7:]).unlink(missing_ok=True)
                except Exception as e:
                    colors.warn(f"[{arch}] Warning: Failed to remove {p}: {e}")
            report_file.unlink()
    
    # 2. Delete the repository directory
    repo_dir = Path(target["repo_dir"])
    if repo_dir.exists():
        print(f"Deleting repository directory: {repo_dir}")
        shutil.rmtree(repo_dir)
        
    colors.info(f"Repository '{name}' removed successfully.")
    return True

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
        help="Build specific component(s) by name. If no name is provided, equivalent to --build-all.",
    )
    parser.add_argument(
        "--build-all",
        action="store_true",
        help="Build all tools and all repo components.",
    )
    parser.add_argument(
        "--rebase",
        nargs="?",
        const="ALL",
        help="Rebase the local branch onto the upstream branch (with squash) and push to una. Optional: specify a single repo name.",
    )
    parser.add_argument(
        "--arch",
        default="x32",
        help="Target architecture(s), comma-separated (e.g., x32,x86_64,aarch64,riscv64). Default: x32",
    )

    parser.add_argument(
        "--kconfig",
        help="Path to kernel configuration file. Defaults to confs/kernel.[arch].config",
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
        "--skel-etc-override",
        metavar="PATH",
        help="Override the default skel/etc directory with the content of PATH in the final system image.",
    )
    parser.add_argument(
        "--conf",
        default="confs/default.conf",
        help="Path to the repository configuration file. Default: confs/default.conf",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Generate a summary of changes in the repositories.",
    )

    args = parser.parse_args()
    
    arches = [a.strip() for a in args.arch.split(",")]
    tools_install_dir = bld_base / "tools"
    test_disk = bld_base / "test.img"

    if args.create_disk:
        create_test_disk(test_disk)

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    # Configuration for components and repositories
    conf_path = BASE_DIR / args.conf
    repos_config, global_cfg = load_repo_config(conf_path)
    
    # Identify repositories to remove (those in filesystem but not in config)
    scanned = scan_repos()
    config_repo_names = {r["name"] for r in repos_config}
    
    for s_cfg in scanned:
        if s_cfg["name"] not in config_repo_names:
            colors.warn(f"Repository '{s_cfg['name']}' found in repo/ but not in config. Removing...")
            remove_repo(s_cfg["name"], scanned, arches)

    # Ensure any directory in repo/ not in our active config is removed completely
    repo_base = BASE_DIR / "repo"
    if repo_base.exists():
        valid_repo_dirs = {Path(r["repo_dir"]).absolute() for r in repos_config if not r.get("is_virtual")}
        for d in repo_base.iterdir():
            if d.is_dir() and d.absolute() not in valid_repo_dirs:
                colors.warn(f"Unreferenced directory '{d.name}' found in repo/. Removing...")
                shutil.rmtree(d, ignore_errors=True)

    # Automatic Sync/Init for repos
    una_base = get_git_remote_base()
    
    for cfg in repos_config:
        # Skip cloning for virtual aliases
        if cfg.get("is_virtual"):
            continue
            
        repo_dir = Path(cfg["repo_dir"])
            
        # We ALWAYS want to ensure remotes are correctly configured (URLs, refspecs)
        # but we only want to perform a destructive RESET if the repo is missing.
        needs_reset = not repo_dir.exists()
        
        if needs_reset:
            if not una_base:
                colors.warn(f"Warning: New repository '{cfg['name']}' found in config but 'una' base URL is unknown. "
                      "Please ensure the top-level repository has a remote named 'una' (e.g., git remote rename origin una). "
                      "Skipping initialization.")
                continue
            colors.info(f"New repository '{cfg['name']}' detected. Initializing...")
        
        base = una_base or "UNKNOWN_BASE"
        if not base.endswith("/") and not base.endswith(":"):
            base += "/"
        una_url = f"{base}{cfg['una_repo']}"
        
        has_origin = "origin_url" in cfg
        init_or_reset_repo(
            repo_dir=repo_dir, 
            origin_url=cfg.get("origin_url"), 
            una_url=una_url, 
            sparse_ignore_dirs=cfg["sparse_ignore_dirs"],
            with_origin=has_origin,
            reset=needs_reset,
            tag=cfg.get("tag")
        )
        if needs_reset:
            save_repo_state(cfg)

    repos = []
    for r in repos_config:
        if r.get("is_virtual", True):
            continue
        config = r.copy()
        # Merge behavior driven by config if merge is enabled
        if una_base:
            base = una_base
            if not base.endswith("/") and not base.endswith(":"):
                base += "/"
            config["una_url"] = f"{base}{r['una_repo']}"
        else:
            config["una_url"] = "UNKNOWN_BASE" 
        repos.append(config)

    build_all = args.build_all

    if args.build is not None and len(args.build) == 0:
        if 'components' in global_cfg:
            args.build = [c.strip() for c in global_cfg['components'].replace(',', ' ').split() if c.strip()]
        else:
            build_all = True

    import graphlib
    dep_graph = {r["name"]: r.get("depends", []) for r in repos}

    if args.build is not None and not build_all:
        required_names = set(args.build)
        missing_targets = required_names - set(dep_graph.keys())
        if missing_targets:
            colors.error(f"Error: Component(s) not found: {', '.join(missing_targets)}")
            sys.exit(1)
            
        queue = list(required_names)
        while queue:
            curr = queue.pop(0)
            for dep in dep_graph.get(curr, []):
                if dep not in required_names:
                    required_names.add(dep)
                    queue.append(dep)
                    
        pruned_graph = {k: [d for d in v if d in required_names] for k, v in dep_graph.items() if k in required_names}
    else:
        required_names = set(dep_graph.keys())
        pruned_graph = dep_graph

    try:
        ts = graphlib.TopologicalSorter(pruned_graph)
        build_order = list(ts.static_order())
    except graphlib.CycleError as e:
        colors.error(f"Error: Circular dependency detected: {e}")
        sys.exit(1)

    repos_to_process = []
    for name in build_order:
        repo = next((r for r in repos if r["name"] == name), None)
        if repo:
            repos_to_process.append(repo)

    # Check if repos exist before building or rebasing
    if (args.build is not None or args.rebase):
        missing = [r["name"] for r in repos_to_process if not r.get("is_virtual") and not Path(r["repo_dir"]).exists()]
        if missing:
            colors.warn(f"Warning: The following repository directories are missing: {', '.join(missing)}")
            print("These should have been initialized automagically if a base URL was available.")
            sys.exit(1)

    if args.list:
        target_type = None if args.list == "all" else args.list
        list_repos(repos, target_type)
        return

    if args.status:
        import subprocess
        print("=== Top-level Repository (una) ===")
        subprocess.run(["git", "status", "-sb"], cwd=BASE_DIR)
        
        processed_dirs = set()
        for r in repos:
            r_path = Path(r["repo_dir"]).absolute()
            if r_path in processed_dirs:
                continue
            
            # Identify if it's a git repo
            if r_path.exists() and (r_path / ".git").exists():
                print(f"\n=== Repository: {r['name']} ({r['repo_dir']}) ===")
                subprocess.run(["git", "status", "-sb"], cwd=r_path)
            elif r_path.exists():
                print(f"\n=== Repository: {r['name']} ({r['repo_dir']}) [Not a Git Repo] ===")
            else:
                print(f"\n=== Repository: {r['name']} ({r['repo_dir']}) [MISSING] ===")
            processed_dirs.add(r_path)


    if args.build is not None or build_all:
        colors.info("Starting build process.")
        all_possible_arches = ["x32", "x86_64", "aarch64", "riscv64"]
        
        tools_marker = tools_install_dir / ".tools_built"
        needs_tools = not tools_marker.exists()
        
        explicit_tools_to_build = [r for r in repos_to_process if r.get("type") == "tools"]
        
        tools_to_build = []
        if build_all and needs_tools:
            tools_to_build = [r for r in repos if r.get("type") == "tools"]
        elif explicit_tools_to_build:
            tools_to_build = explicit_tools_to_build
        elif needs_tools:
            # A target requires tools, and marker is missing
            target_configs_to_build = [r for r in repos_to_process if r.get("type") != "tools"]
            if target_configs_to_build:
                tools_to_build = [r for r in repos if r.get("type") == "tools"]

        if tools_to_build:
            colors.info("\n--- Tools Stage ---")
            for r in tools_to_build:
                module = load_repo_una(r["repo_dir"], r.get("una_file", "una.py"))
                if hasattr(module, "tools_configure"): module.tools_configure(tools_install_dir, arches=all_possible_arches)
                if hasattr(module, "tools_build"): module.tools_build(tools_install_dir)
                if hasattr(module, "tools_install"): module.tools_install(tools_install_dir)
            tools_marker.parent.mkdir(parents=True, exist_ok=True)
            tools_marker.write_text("tools up-to-date\n")

        target_configs_to_build = [r for r in repos_to_process if r.get("type") != "tools"]
        for arch in arches:
            colors.info(f"\n====== Target Stage: {arch} ======")
            arch_bld_dir = bld_base / arch
            staging_dir = arch_bld_dir / "staging"
            target_dir = arch_bld_dir / "target"
            runner = StepRunner(arch, staging_dir, target_dir)

            # Ensure build directories exist and skel is propagated
            staging_dir.mkdir(parents=True, exist_ok=True)
            target_dir.mkdir(parents=True, exist_ok=True)
            if skel_dir.exists():
                colors.info(f"[{arch}] Phase -1: Skeleton Propagation (verified)")
                skel_runner = StepRunner(arch, staging_dir, target_dir)
                skel_runner.run_step(
                    cfg={"name": "skel"}, 
                    step_name="propagate", 
                    step_func=propagate_skel
                )
            else:
                colors.warn(f"[{arch}] No skel directory found - empty staging/target")

            # Clean ALL target repos before build to avoid stale configs/artifacts between arches
            # This is critical for the kernel which relies on its .config in the source tree
            cleaned_dirs = set()
            tools_dirs = {Path(r["repo_dir"]).absolute() for r in repos if r["type"] == "tools"}
            all_target_repos = [r for r in repos if r.get("type") != "tools"]
            
            for r in all_target_repos:
                r_path = Path(r["repo_dir"]).absolute()
                if r_path in cleaned_dirs: continue
                if r_path in tools_dirs:
                    colors.info(f"[{arch}] Skipping git clean for {r['name']} (shared with tools components)")
                    continue
                
                colors.info(f"[{arch}] Cleaning {r['name']} ({r['repo_dir']})...")
                import subprocess
                if is_repo_dirty(r_path):
                    colors.error(f"[{arch}] ERROR: Repository {r['name']} is dirty. Please commit or stash changes before building.")
                    sys.exit(1)
                subprocess.run(["git", "clean", "-fdx"], cwd=r_path, check=True)
                # Ensure submodules are also cleaned to avoid arch-mismatch in static libs (e.g. nsd -> simdzone)
                if (r_path / ".gitmodules").exists():
                    try:
                        subprocess.run(["git", "submodule", "foreach", "--recursive", "git", "clean", "-fdx"], cwd=r_path, check=True)
                    except subprocess.CalledProcessError:
                        # Submodules might not be initialized yet, which is fine
                        pass
                cleaned_dirs.add(r_path)

            target_triple = get_target_triple(arch)
            march = get_arch_flags(arch)
            ld_musl = f"/usr/lib/ld-musl-{arch}.so.1"
            if arch == "x32":
                ld_musl = "/usr/lib/ld-musl-x32.so.1"
            elif arch == "x86_64":
                ld_musl = "/usr/lib/ld-musl-x86_64.so.1"

            musl_cfg = arch_bld_dir / "musl.cfg"
            musl_cxx_cfg = arch_bld_dir / "musl_c++.cfg"
            musl_static_cfg = arch_bld_dir / "musl_static.cfg"
            
            if not musl_cfg.exists() or not musl_cxx_cfg.exists() or not musl_static_cfg.exists():
                colors.info(f"[{arch}] Generating compiler configurations...")
                arch_bld_dir.mkdir(parents=True, exist_ok=True)
                lld_path = tools_install_dir / "bin" / "ld.lld"
                lib_p = staging_dir / "usr" / "lib"
                
                # Common flags (excluding system includes to control order)
                common_flags = f"--target={target_triple}\n--sysroot={staging_dir}\n-fPIE\n{march}\n"
                
                # Pure C Config
                musl_cfg.write_text(f"{common_flags}-isystem {staging_dir}/usr/include\n-fuse-ld={lld_path}\n-nostdlib\n-L{staging_dir}/usr/lib\n-lc\n-Wl,-dynamic-linker,{ld_musl}\n")
                
                # C++ Config (MUST have c++/v1 before usr/include)
                musl_cxx_cfg.write_text(f"{common_flags}-isystem {staging_dir}/usr/include/c++/v1\n-isystem {staging_dir}/usr/include\n--ld-path={lld_path}\n-nostdlib\n{lib_p}/Scrt1.o\n{lib_p}/crti.o\n-L{lib_p}\n-lc++\n-lc++abi\n-lunwind\n-lc\n{lib_p}/crtn.o\n-Wl,-dynamic-linker,{ld_musl}\n")
                
                # Static Config
                musl_static_cfg.write_text(f"{common_flags}-isystem {staging_dir}/usr/include\n-fuse-ld={lld_path}\n-nostdlib\n{lib_p}/Scrt1.o\n{lib_p}/crti.o\n-L{lib_p}\n-lc\n{lib_p}/crtn.o\n-Wl,-dynamic-linker,{ld_musl}\n")

            cpu_flags = global_cfg.get('cpu_flags', '')
            os.environ["CFLAGS"] = f"--config={musl_cfg} -pipe -D_FILE_OFFSET_BITS=64 {cpu_flags}"
            os.environ["CXXFLAGS"] = f"--config={musl_cxx_cfg} -pipe -D_FILE_OFFSET_BITS=64 {cpu_flags}"
            os.environ["CFLAGS_STATIC"] = f"--config={musl_static_cfg} -pipe -D_FILE_OFFSET_BITS=64 {cpu_flags}"
            os.environ["CPPFLAGS"] = f"-D_FILE_OFFSET_BITS=64 {cpu_flags}"

            colors.info(f"[{arch}] Building Target Components in Dependency Order")
            for r in target_configs_to_build:
                if r.get("is_virtual"):
                    continue

                colors.info(f"[{arch}] Processing component: {r['name']}")
                
                if r["name"] == "linux-image":
                    skel_etc = None
                    if args.skel_etc_override:
                        skel_etc = Path(args.skel_etc_override)
                    elif 'etc_dir' in global_cfg:
                        skel_etc = BASE_DIR / global_cfg['etc_dir']
                    else:
                        skel_etc = skel_dir / "etc"

                    if skel_etc.exists():
                        colors.info(f"[{arch}] Finalizing: Replacing /etc with {skel_etc} before kernel build...")
                        shutil.rmtree(staging_dir / "etc", ignore_errors=True)
                        shutil.rmtree(target_dir / "etc", ignore_errors=True)
                        shutil.copytree(skel_etc, staging_dir / "etc", symlinks=True)
                        shutil.copytree(skel_etc, target_dir / "etc", symlinks=True)
                    elif args.skel_etc_override or 'etc_dir' in global_cfg:
                        colors.error(f"[{arch}] Error: Skel etc override path {skel_etc} does not exist.")
                        sys.exit(1)

                module = load_repo_una(r["repo_dir"], r.get("una_file", "una.py"))
                kwargs = {"arch": arch}
                
                if r["name"] in ["linux-headers", "linux-image"]:
                    kconfig = args.kconfig
                    if not kconfig and 'kconfig' in global_cfg:
                        kconfig = BASE_DIR / global_cfg['kconfig'].replace("<arch>", arch)
                    if not kconfig:
                        kconfig = BASE_DIR / "confs" / f"kernel.{arch}.config"
                    kwargs["kconfig"] = Path(kconfig).absolute()

                if hasattr(module, "target_configure"): runner.run_step(r, "target_configure", module.target_configure, **kwargs)
                if hasattr(module, "target_headers_install"): runner.run_step(r, "target_headers_install", module.target_headers_install, **kwargs)
                if hasattr(module, "target_build"): runner.run_step(r, "target_build", module.target_build, **kwargs)
                if hasattr(module, "target_install"): runner.run_step(r, "target_install", module.target_install, **kwargs)
                
                if r["name"] == "linux-image" and "kernel_image" in r:
                    image_map = r["kernel_image"]
                    if arch in image_map:
                        rel_path = image_map[arch]
                        src_img = Path(r["repo_dir"]) / rel_path
                        
                        kernel_name = r.get("kernel_name", "kernel.<arch>")
                        dest_img = bld_base / kernel_name.replace("<arch>", arch)
                        
                        if src_img.exists():
                            print(f"[{arch}] Copying kernel image to {dest_img}")
                            shutil.copy(src_img, dest_img)
                        else:
                            print(f"[{arch}] Warning: Kernel image not found at {src_img}")
                        
                        # Sync back updated config to source
                        src_config = Path(r["repo_dir"]) / ".config"
                        if src_config.exists():
                            kconfig_path = kwargs.get("kconfig")
                            print(f"[{arch}] Syncing back sanitized updated kernel config to {kconfig_path}")
                            sync_kernel_config(src_config, kconfig_path)
                    else:
                        print(f"[{arch}] Warning: No kernel image path defined for this architecture")


        # Post-build cleanup for workspace repositories
        print("\n--- Post-build Workspace Cleanup ---")
        cleaned_dirs = set()
        for r in repos:
            r_path = Path(r["repo_dir"]).absolute()
            if r_path in cleaned_dirs: continue
            if r_path.exists() and (r_path / ".git").exists():
                print(f"Cleaning {r['name']} ({r['repo_dir']})...")
                if is_repo_dirty(r_path):
                    print(f"ERROR: Repository {r['name']} is dirty. Skipping post-build cleanup for this repo.")
                    continue
                import subprocess
                subprocess.run(["git", "clean", "-fdx"], cwd=r_path, check=True)
                if (r_path / ".gitmodules").exists():
                    try:
                        subprocess.run(["git", "submodule", "foreach", "--recursive", "git", "clean", "-fdx"], cwd=r_path, check=True)
                    except subprocess.CalledProcessError:
                        pass
                cleaned_dirs.add(r_path)

    if args.run:
        target_name = "linux-image"
        proj = next((r for r in repos if r["name"] == target_name), None)
        if not proj:
            print(f"Error: Component '{target_name}' not found.")
            sys.exit(1)
        
        if len(arches) > 1:
            print("Error: --run only supports one architecture at a time.")
            sys.exit(1)
        
        arch = arches[0]
        print(f"\n--- Run Stage: {target_name} ({arch}) ---")
        
        kernel_name = proj.get("kernel_name", "kernel.<arch>")
        kernel_img = bld_base / kernel_name.replace("<arch>", arch)
        if not kernel_img.exists():
            print(f"Error: Kernel image not found at {kernel_img}. Please build it first with --build {target_name}.")
            sys.exit(1)

        import subprocess
        qemu_cmd = {
            "x32": [
                "qemu-system-x86_64", "-enable-kvm", "-no-reboot", "-m", "1G", "-machine", "q35", "-cpu", "host",
                "-drive", "if=pflash,format=raw,readonly=on,file=/etc/bios/OVMF.fd",
                "-serial", "mon:stdio",
                "-netdev", "user,id=vmnic,restrict=n,hostfwd=tcp::2022-:22", "-device", "virtio-net-pci,romfile=,netdev=vmnic",
                "-nodefaults", "-nographic",
                "-kernel", str(kernel_img), "-append", "console=ttyS0"
            ],
            "x86_64": [
                "qemu-system-x86_64", "-enable-kvm", "-no-reboot", "-m", "1G", "-machine", "q35", "-cpu", "host",
                "-drive", "if=pflash,format=raw,readonly=on,file=/etc/bios/OVMF.fd",
                "-serial", "mon:stdio",
                "-netdev", "user,id=vmnic,restrict=n", "-device", "virtio-net-pci,romfile=,netdev=vmnic",
                "-nodefaults", "-nographic",
                "-kernel", str(kernel_img), "-append", "console=ttyS0"
            ],
            "aarch64": [
                "qemu-system-aarch64", "-no-reboot", "-M", "virt", "-cpu", "cortex-a53", "-m", "1G",
                "-serial", "mon:stdio",
                "-netdev", "user,id=vmnic,restrict=n", "-device", "virtio-net-pci,romfile=,netdev=vmnic",
                "-nodefaults", "-nographic",
                "-kernel", str(kernel_img), "-append", "console=ttyAMA0"
            ],
            "riscv64": [
                "qemu-system-riscv64", "-no-reboot", "-M", "virt", "-m", "1G",
                "-serial", "mon:stdio",
                "-netdev", "user,id=vmnic,restrict=n", "-device", "virtio-net-pci,romfile=,netdev=vmnic",
                "-nodefaults", "-nographic",
                "-kernel", str(kernel_img), "-append", "console=ttyS0"
            ]
        }

        cmd = qemu_cmd.get(arch)
        if not cmd:
            print(f"Error: No run configuration for architecture: {arch}")
            sys.exit(1)

        test_disk = bld_base / "test.img"
        if test_disk.exists():
            print(f"Adding test disk {test_disk} to QEMU...")
            cmd += ["-drive", f"file={test_disk},format=raw,if=virtio"]

        print(f"Executing: {' '.join(cmd)}")
        try:
            subprocess.run(cmd, check=True)
        except KeyboardInterrupt:
            print("\nKernel execution stopped by user.")
        except Exception as e:
            print(f"Error during kernel execution: {e}")
            sys.exit(1)

    tag = args.save
    if tag or args.rebase:
        from git import Repo
        processed_dirs = set()

        # Handle top-level repository
        top_repo_path = BASE_DIR
        if top_repo_path not in processed_dirs:
            # Only rebase top-level if we are doing ALL or if explicitly named "una"
            # or if we are doing a global --save (tag is set)
            if tag or args.rebase == "ALL" or args.rebase == "una":
                print(f"\n--- Top-level Repository (una) ---")
                top_repo = Repo(top_repo_path)
                # Fetch from 'una' remote
                print("Fetching from 'una'...")
                top_repo.remotes.una.fetch(progress=TqdmProgress())
                
                # For top-level, we assume 'una' branch rebasing onto 'una/una'
                target_branch = "una/una" 
                if tag:
                    save_and_push(top_repo, target_branch, tag, remote_name="una")
                elif args.rebase:
                    rebase_and_push(top_repo, target_branch, remote_name="una", squash=True)
                processed_dirs.add(top_repo_path)

        # Handle sub-repositories
        for cfg in repos_to_process:
            # Only rebase if we are doing ALL or if this repo matches the name
            # or if we are doing a global --save (tag is set)
            if not (tag or args.rebase == "ALL" or args.rebase == cfg["name"]):
                continue

            r_path = Path(cfg["repo_dir"]).absolute()
            if r_path in processed_dirs: continue
            
            print(f"\n--- Repository: {cfg['name']} ({cfg['repo_dir']}) ---")
            repo = Repo(r_path)
            remote_prefix = "origin" if "origin_url" in cfg else "una"
            
            # Automatic fetch before rebase/tag
            print(f"Fetching from {remote_prefix}...")
            repo.remotes[remote_prefix].fetch(progress=TqdmProgress())
            if remote_prefix == "origin" and "una" in repo.remotes:
                print("Also fetching from una...")
                repo.remotes.una.fetch(progress=TqdmProgress())
            
            if remote_prefix == "una":
                target_branch = "una/una"
            else:
                # Target selection for rebase/save:
                # 1. Explicitly configured branch override
                # 2. Configured version tag (pins the rebase to a specific release)
                # 3. Discovery of the remote HEAD (master/main)
                branch = cfg.get("branch")
                tag_name = cfg.get("tag")
                
                if branch:
                    target_branch = f"{remote_prefix}/{branch}"
                elif tag_name:
                    target_branch = tag_name
                else:
                    target_branch = f"{remote_prefix}/{get_remote_head(repo, remote_prefix)}"
            
            if tag:
                save_and_push(repo, target_branch, tag)
            elif args.rebase:
                rebase_and_push(repo, target_branch, squash = True, tag = cfg.get("tag"))
            processed_dirs.add(r_path)

    if args.checkout:
        from git import Repo
        tag_to_checkout = args.checkout
        processed_dirs = set()

        # Handle top-level repository
        top_repo_path = BASE_DIR
        if top_repo_path not in processed_dirs:
            print(f"\n--- Top-level Repository (una) ---")
            top_repo = Repo(top_repo_path)
            print(f"Fetching tags for top-level repo...")
            top_repo.remotes.una.fetch(tags=True)
            
            # Automatic fetch before rebase/tag or on explicit request
            if tag_to_checkout or args.rebase:
                print(f"Fetching from una...")
                top_repo.remotes.una.fetch(progress=TqdmProgress())

            if args.rebase:
                rebase_and_push(top_repo, "una/una", remote_name="una", squash=True)
            elif tag_to_checkout:
                print(f"Checking out tag '{tag_to_checkout}'...")
                try:
                    top_repo.git.checkout(tag_to_checkout)
                except Exception as e:
                    print(f"Error checking out tag '{tag_to_checkout}' in top-level repo: {e}")
            processed_dirs.add(top_repo_path)

        # Handle sub-repositories
        for cfg in repos_to_process:
            r_path = Path(cfg["repo_dir"]).absolute()
            if r_path in processed_dirs: continue
            
            print(f"\n--- Repository: {cfg['name']} ({cfg['repo_dir']}) ---")
            repo = Repo(r_path)
            print(f"Fetching tags...")
            repo.remotes.una.fetch(tags=True)
            print(f"Checking out tag '{tag_to_checkout}'...")
            try:
                repo.git.checkout(tag_to_checkout)
            except Exception as e:
                print(f"Error checking out tag '{tag_to_checkout}' in {cfg['name']}: {e}")
            processed_dirs.add(r_path)

    if args.clean:
        print("\n=== Global Cleanup ===")
        import subprocess
        
        # 1. Clean build directory (staging and target, but keep reports?)
        # User said "removes all files produced by the build".
        # Reports are also produced by the build but useful for next build cleanup.
        # Let's keep 'report' directory but clean staging/target.
        for arch in arches:
            arch_bld_dir = bld_base / arch
            staging_dir = arch_bld_dir / "staging"
            target_dir = arch_bld_dir / "target"
            
            if staging_dir.exists():
                print(f"Cleaning {staging_dir}...")
                shutil.rmtree(staging_dir)
                staging_dir.mkdir(parents=True, exist_ok=True)
            if target_dir.exists():
                print(f"Cleaning {target_dir}...")
                shutil.rmtree(target_dir)
                target_dir.mkdir(parents=True, exist_ok=True)
        
        # 2. Clean workspace sub-repos
        cleaned_dirs = set()
        for r in repos:
            r_path = Path(r["repo_dir"]).absolute()
            if r_path in cleaned_dirs: continue
            if r_path.exists() and (r_path / ".git").exists():
                print(f"Cleaning {r['name']} ({r['repo_dir']})...")
                if is_repo_dirty(r_path):
                    print(f"ERROR: Repository {r['name']} is dirty. Stopping global cleanup.")
                    sys.exit(1)
                subprocess.run(["git", "clean", "-fdx"], cwd=r_path, check=True)
                cleaned_dirs.add(r_path)
                
        # 2.5 Clean unreferenced repos in repo/
        repo_base = BASE_DIR / "repo"
        if repo_base.exists():
            valid_repo_dirs = {Path(r["repo_dir"]).absolute() for r in repos if not r.get("is_virtual")}
            for d in repo_base.iterdir():
                if d.is_dir() and d.absolute() not in valid_repo_dirs:
                    print(f"Removing unreferenced directory {d}...")
                    shutil.rmtree(d, ignore_errors=True)
                    
        # 3. Clean top-level workspace (excluding reports, kernel images, and repos)
        print("Cleaning top-level workspace...")
        if is_repo_dirty(BASE_DIR):
            print("ERROR: Top-level repository is dirty. Stopping global cleanup.")
            sys.exit(1)
        subprocess.run(["git", "clean", "-xfd", "-e", "bld/", "-e", "repo/"], cwd=BASE_DIR)

    if args.report:
        from git import Repo
        import subprocess

        print("\n=== Generating Change Report ===")
        report_dir = bld_base / "report"
        report_dir.mkdir(parents=True, exist_ok=True)

        summary_file = report_dir / "summary.txt"
        summary_lines = []

        processed_dirs = set()
        
        # Include all repositories from configuration
        all_repos_to_report = repos

        for cfg in all_repos_to_report:
            r_path = Path(cfg["repo_dir"]).absolute()
            if r_path in processed_dirs: continue
            if not r_path.exists() or not (r_path / ".git").exists():
                continue

            repo = Repo(r_path)
            name = cfg["name"]

            # Determine target branch/tag (consistent with rebase logic)
            remote_prefix = "origin" if "origin_url" in cfg else "una"
            branch = cfg.get("branch")
            tag_name = cfg.get("tag")
            if branch:
                target = f"{remote_prefix}/{branch}"
            elif tag_name:
                target = tag_name
            else:
                target = f"{remote_prefix}/{get_remote_head(repo, remote_prefix)}"

            print(f"[{name}] Comparing against {target}...")

            try:
                # 1. Get diffstat
                diffstat = repo.git.diff(target, "--stat")
                # 2. Get full diff
                full_diff = repo.git.diff(target)
                
                # If there are no changes, we still record it
                if not diffstat.strip():
                    diffstat = "No changes."
                    full_diff = ""

                # Write full diff to a file
                diff_filename = f"{name}.diff"
                diff_file = report_dir / diff_filename
                diff_file.write_text(full_diff)

                # Add to summary
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
