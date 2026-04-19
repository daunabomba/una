#!/usr/bin/python

import argparse
import shutil
import os
import sys
from pathlib import Path

# Add the script's directory to sys.path so 'mods' can be imported from anywhere
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from mods.utils import init_or_reset_repo, rebase_and_push, save_and_push, get_target_triple, get_arch_flags
from mods.snapshot import take_snapshot, compare_snapshots, write_report, get_report_paths

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


class StepRunner:
    def __init__(self, arch, staging_dir, target_dir):
        self.arch = arch
        self.staging_dir = staging_dir
        self.target_dir = target_dir
        self.component_snapshots = {} # name -> {staging: {}, target: {}}
        self.cleaned_components = set()

    def run_step(self, cfg, step_name, step_func, **kwargs):
        name = cfg["name"]
        print(f"[{self.arch}] Running {name}::{step_name}...")
        
        # 1. Cleanup and Pre-snapshot on first call for this component
        if name not in self.cleaned_components:
            report_file = bld_base / self.arch / "report" / f"{name}.txt"
            if report_file.exists():
                print(f"[{self.arch}] Cleaning up previous build outputs for {name}...")
                paths = get_report_paths(report_file)
                for p in paths:
                    try:
                        if p.startswith("staging/"):
                            (self.staging_dir / p[8:]).unlink(missing_ok=True)
                        elif p.startswith("target/"):
                            (self.target_dir / p[7:]).unlink(missing_ok=True)
                    except Exception as e:
                        print(f"[{self.arch}] Warning: Failed to remove {p}: {e}")
            
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
            print(f"[{self.arch}] ERROR: {name} modified or deleted files in staging!")
        if mod_t or del_t:
            print(f"[{self.arch}] ERROR: {name} modified or deleted files in target!")
            
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
    filtered = [r for r in repos if target_type is None or r.get("type") == target_type]
    for r in filtered:
        script_info = f" (Script: {r.get('una_file', 'una.py')})"
        rebase_info = " [Rebase: Yes]" if r.get("rebase", False) else " [Rebase: No]"
        print(f"[{r.get('type', 'unknown')}] {r['name']} -> {r['repo_dir']}{script_info}{rebase_info}")
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
        print(f"Error creating test disk: {e}")
        if disk_path.exists(): disk_path.unlink()
        raise
    finally:
        if p1_img.exists(): p1_img.unlink()
        if p2_img.exists(): p2_img.unlink()

def propagate_skel(staging_dir, target_dir):
    """Skel propagation using original file-by-file method + snapshot verification"""
    import subprocess

    print("Propagating skeleton (original method)...")

    for dest in [staging_dir, target_dir]:
        # ORIGINAL logic: Handle ONLY symlink/dir conflicts
        for item in os.listdir(skel_dir):
            s_item = skel_dir / item
            d_item = dest / item

            if d_item.exists() and s_item.is_symlink() and d_item.is_dir() and not d_item.is_symlink():
                print(f"Removing conflicting directory {d_item} to preserve skel symlink.")
                shutil.rmtree(d_item)

        # ORIGINAL cp -a --remove-destination (robust merge)
        subprocess.run([
            "cp", "-a", "--remove-destination",
            f"{skel_dir}/.", str(dest)
        ], check=True)

def main():
    parser = argparse.ArgumentParser(
        description="Initialize or reset Git repos from a list.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without cloning/resetting.",
    )
    git_base = get_git_remote_base()
    parser.add_argument(
        "--init",
        nargs="?",
        const=git_base or "DETECT_FAILED",
        metavar="BASE_URL",
        help=f"Initialize or reinit repos with the specified 'una' base URL. Defaults to the current repository's remote base ({git_base}) if not specified.",
    )
    parser.add_argument(
        "--init-with-origin",
        action="store_true",
        help="Include original upstream remotes and enable rebasing projects to them during initialization.",
    )
    parser.add_argument(
        "--list",
        choices=["host", "base", "other", "all"],
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
        help="Rebase the local 'una' branch onto its configured upstream una branch and push to una.",
    )
    parser.add_argument(
        "--arch",
        default="x32",
        help="Target architecture(s), comma-separated (e.g., x32,x86_64,aarch64,riscv64). Default: x32",
    )
    parser.add_argument(
        "--no-build-host",
        action="store_true",
        help="Skip building host tools.",
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
        help="Stage all changes, commit with the provided message, then rebase and push for all repositories.",
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
        "--git-config",
        action="append",
        metavar="key=value",
#-git-config core.sshCommand="ssh -i ~/.github.key -o IdentitiesOnly=yes"
        help="Pass a configuration parameter to git (e.g., --git-config core.sshCommand='...'). Can be specified multiple times.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove all files produced by the build and clean the workspace repositories.",
    )

    args = parser.parse_args()
    
    if args.git_config:
        config_pairs = [f"'{c}'" for c in args.git_config]
    else:
        config_pairs = []

    # Always add sparse‑checkout defaults
    config_pairs.append("'core.sparseCheckout=true'")
    config_pairs.append("'index.sparse=true'")
    config_pairs.append("'core.sparseCheckoutCone=false'")

    os.environ["GIT_CONFIG_PARAMETERS"] = " ".join(config_pairs)
    
    arches = [a.strip() for a in args.arch.split(",")]
    host_install_dir = bld_base / "host"
    test_disk = bld_base / "test.img"

    if args.create_disk:
        create_test_disk(test_disk)

    if args.init == "DETECT_FAILED":
        print("Error: --init was used without a BASE_URL, and no git remote 'una' was detected.")
        print("Please rename your remote to 'una' and checkout 'una' or 'una/branch name':")
        print("  git remote rename <origin_name> una")
        sys.exit(1)

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    # Configuration for components and repositories
    repos_config = [
        {
            "name": "llvm-host",
            "una_repo": "llvm-project.git",
            "repo_dir": BASE_DIR / "repo/llvm",
            "una_file": "una/host.py",
            "origin_url": "/mnt/work/bld/llvm-project.git",
#            "origin_url": "https://github.com/llvm/llvm-project.git",
            "type": "host",
            "branch": "main",
            "rebase": True,
            "sparse_ignore_dirs": [],
        },
#        {
#            "name": "rust",
#            "una_repo": "rust.git", 
#            "repo_dir": BASE_DIR / "repo/rust",
#            "una_file": "una.py",
#            "origin_url": "/mnt/work/bld/rust.git",
#            "origin_url": "https://github.com/rust-lang/rust.git",
#            "type": "host",
#            "branch": "master",
#            "rebase": True,
#            "sparse_ignore_dirs": [],
#        },
        {
            "name": "linux-headers",
            "una_repo": "linux.git", 
            "repo_dir": BASE_DIR / "repo/kernel",
            "una_file": "una/headers.py",
            "origin_url": "/mnt/work/bld/linux-stable.git",
#            "origin_url": "https://kernel.googlesource.com/pub/scm/linux/kernel/git/stable/linux-stable.git",
            "type": "base", 
            "branch": "master",
            "rebase": True,
            "sparse_ignore_dirs": ["Documentation", "arch/arm/boot/dts"],
        },
        {
            "name": "musl",
            "una_repo": "musl.git",
            "repo_dir": BASE_DIR / "repo/musl",
            "origin_url": "/mnt/work/bld/musl.git",
#            "origin_url": "https://git.musl-libc.org/git/musl",
            "type": "base",
            "branch": "master",
            "rebase": True,
            "sparse_ignore_dirs": [],
        },
        {
            "name": "llvm-runtime",
            "una_repo": "llvm-project.git",
            "repo_dir": BASE_DIR / "repo/llvm",
            "una_file": "una/runtime.py",
            "origin_url": "/mnt/work/bld/llvm-project.git",
#            "origin_url": "https://github.com/llvm/llvm-project.git",
            "type": "base",
            "branch": "main",
            "rebase": True,
            "sparse_ignore_dirs": [],
        },
        {
            "name": "busybox",
            "una_repo": "busybox.git",
            "repo_dir": BASE_DIR / "repo/busybox",
            "origin_url": "/mnt/work/bld/busybox.git",
#            "origin_url": "https://git.busybox.net/busybox",
            "type": "other",
            "branch": "master",
            "rebase": True,
            "sparse_ignore_dirs": [],
        },
        {
            "name": "openssl",
            "una_repo": "openssl.git",
            "repo_dir": BASE_DIR / "repo/openssl",
            "origin_url": "/mnt/work/bld/openssl.git",
#            "origin_url": "https://github.com/openssl/openssl.git",
            "type": "other",
            "branch": "master",
            "rebase": True,
            "sparse_ignore_dirs": [],
        },
        {
            "name": "nsd",
            "una_repo": "nsd.git",
            "repo_dir": BASE_DIR / "repo/nsd",
            "origin_url": "https://github.com/NLnetLabs/nsd.git",
            "type": "other",
            "branch": "master",
            "rebase": True,
            "sparse_ignore_dirs": [],
        },
        {
            "name": "mxmux",
            "una_repo": "mxmux.git",
            "repo_dir": BASE_DIR / "repo/mxmux",
            "origin_url": "https://github.com/daunabomba/mxmux.git",
            "type": "other",
            "branch": "master",
            "rebase": False,
            "sparse_ignore_dirs": [],
        },
        {
            "name": "dropbear",
            "una_repo": "dropbear.git",
            "repo_dir": BASE_DIR / "repo/dropbear",
            "origin_url": "https://github.com/mkj/dropbear.git",
            "type": "other",
            "branch": "master",
            "rebase": True,
            "sparse_ignore_dirs": [],
        },
        {
            "name": "wireguard-tools",
            "una_repo": "wireguard-tools.git",
            "repo_dir": BASE_DIR / "repo/wireguard-tools",
            "origin_url": "https://git.zx2c4.com/wireguard-tools",
            "type": "other",
            "branch": "master",
            "rebase": True,
            "sparse_ignore_dirs": [],
        },
        {
            "name": "libmnl",
            "una_repo": "libmnl.git",
            "repo_dir": BASE_DIR / "repo/libmnl",
            "origin_url": "https://git.netfilter.org/libmnl",
            "type": "base",
            "branch": "master",
            "rebase": True,
            "sparse_ignore_dirs": [],
        },
        {
            "name": "libnftnl",
            "una_repo": "libnftnl.git",
            "repo_dir": BASE_DIR / "repo/libnftnl",
            "origin_url": "https://git.netfilter.org/libnftnl",
            "type": "base",
            "branch": "master",
            "rebase": True,
            "sparse_ignore_dirs": [],
        },
        {
            "name": "nftables",
            "una_repo": "nftables.git",
            "repo_dir": BASE_DIR / "repo/nftables",
            "origin_url": "https://git.netfilter.org/nftables/",
            "type": "other",
            "branch": "master",
            "rebase": True,
            "sparse_ignore_dirs": [],
        },
        {
            "name": "linux-image", 
            "una_repo": "linux.git",
            "repo_dir": BASE_DIR / "repo/kernel",
            "una_file": "una/kernel.py",
            "origin_url": "/mnt/work/bld/linux-stable.git",
#            "origin_url": "https://kernel.googlesource.com/pub/scm/linux/kernel/git/stable/linux-stable.git",
            "type": "other",  # Phase 4: final image
            "branch": "master", 
            "rebase": True,
            "kernel_image": {  # Only here!
                "x32": "arch/x86/boot/bzImage",
                "x86_64": "arch/x86/boot/bzImage", 
                "aarch64": "arch/arm64/boot/Image.gz",
                "riscv64": "arch/riscv/boot/Image",
            },
            "sparse_ignore_dirs": ["Documentation", "arch/arm/boot/dts"],
        },
    ]

    repos = []
    una_base = args.init
    
    for r in repos_config:
        config = r.copy()
        if not args.init_with_origin:
            config["rebase"] = False
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

    if args.init:
        for arch in arches:
            arch_bld_dir = bld_base / arch
            staging_dir = arch_bld_dir / "staging"
            target_dir = arch_bld_dir / "target"

            if args.build == "ALL" or not args.build:
                print(f"Initializing build directories for {arch}...")
                shutil.rmtree(arch_bld_dir, ignore_errors=True)
                host_install_dir.mkdir(parents=True, exist_ok=True)
                staging_dir.mkdir(parents=True, exist_ok=True)
                target_dir.mkdir(parents=True, exist_ok=True)
        
        initialized_dirs = set()
        for cfg in repos_to_process:
            repo_dir = cfg["repo_dir"]
            if repo_dir in initialized_dirs:
                continue
            init_or_reset_repo(
                repo_dir=repo_dir, 
                origin_url=cfg["origin_url"], 
                una_url=cfg["una_url"], 
                sparse_ignore_dirs=cfg["sparse_ignore_dirs"],
                with_origin=args.init_with_origin
            )
            initialized_dirs.add(repo_dir)

    if args.build:
        print("Starting build process.")
        all_possible_arches = ["x32", "x86_64", "aarch64", "riscv64"]
        host_repos = [r for r in repos_to_process if r["type"] == "host"]
        if host_repos and not args.no_build_host:
            print("\n--- Host Stage ---")
            for r in host_repos:
                module = load_repo_una(r["repo_dir"], r.get("una_file", "una.py"))
                if hasattr(module, "host_configure"): module.host_configure(host_install_dir, arches=all_possible_arches)
                if hasattr(module, "host_build"): module.host_build(host_install_dir)
                if hasattr(module, "host_install"): module.host_install(host_install_dir)

        target_configs_to_build = [r for r in repos_to_process if r["type"] in ["base", "other"]]
        for arch in arches:
            print(f"\n====== Target Stage: {arch} ======")
            arch_bld_dir = bld_base / arch
            staging_dir = arch_bld_dir / "staging"
            target_dir = arch_bld_dir / "target"
            runner = StepRunner(arch, staging_dir, target_dir)

            # Ensure build directories exist and skel is propagated
            staging_dir.mkdir(parents=True, exist_ok=True)
            target_dir.mkdir(parents=True, exist_ok=True)
            if skel_dir.exists():
                print(f"[{arch}] Phase -1: Skeleton Propagation (verified)")
                skel_runner = StepRunner(arch, staging_dir, target_dir)
                skel_runner.run_step(
                    cfg={"name": "skel"}, 
                    step_name="propagate", 
                    step_func=propagate_skel
                )
            else:
                print(f"[{arch}] No skel directory found - empty staging/target")

            # Clean ALL target repos before build to avoid stale configs/artifacts between arches
            # This is critical for the kernel which relies on its .config in the source tree
            cleaned_dirs = set()
            host_dirs = {Path(r["repo_dir"]).absolute() for r in repos if r["type"] == "host"}
            all_target_repos = [r for r in repos if r["type"] in ["base", "other"]]
            
            for r in all_target_repos:
                r_path = Path(r["repo_dir"]).absolute()
                if r_path in cleaned_dirs: continue
                if r_path in host_dirs:
                    print(f"[{arch}] Skipping git clean for {r['name']} (shared with host components)")
                    continue
                
                print(f"[{arch}] Cleaning {r['name']} ({r['repo_dir']})...")
                import subprocess
                if is_repo_dirty(r_path):
                    print(f"[{arch}] ERROR: Repository {r['name']} is dirty. Please commit or stash changes before building.")
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
                print(f"[{arch}] Generating compiler configurations...")
                arch_bld_dir.mkdir(parents=True, exist_ok=True)
                lld_path = host_install_dir / "bin" / "ld.lld"
                lib_p = staging_dir / "usr" / "lib"
                
                # Common flags (excluding system includes to control order)
                common_flags = f"--target={target_triple}\n--sysroot={staging_dir}\n-fPIE\n{march}\n"
                
                # Pure C Config
                musl_cfg.write_text(f"{common_flags}-isystem {staging_dir}/usr/include\n-fuse-ld={lld_path}\n-nostdlib\n-L{staging_dir}/usr/lib\n-lc\n-Wl,-dynamic-linker,{ld_musl}\n")
                
                # C++ Config (MUST have c++/v1 before usr/include)
                musl_cxx_cfg.write_text(f"{common_flags}-isystem {staging_dir}/usr/include/c++/v1\n-isystem {staging_dir}/usr/include\n--ld-path={lld_path}\n-nostdlib\n{lib_p}/Scrt1.o\n{lib_p}/crti.o\n-L{lib_p}\n-lc++\n-lc++abi\n-lunwind\n-lc\n{lib_p}/crtn.o\n-Wl,-dynamic-linker,{ld_musl}\n")
                
                # Static Config
                musl_static_cfg.write_text(f"{common_flags}-isystem {staging_dir}/usr/include\n-fuse-ld={lld_path}\n-nostdlib\n{lib_p}/Scrt1.o\n{lib_p}/crti.o\n-L{lib_p}\n-lc\n{lib_p}/crtn.o\n-Wl,-dynamic-linker,{ld_musl}\n")

            os.environ["CFLAGS"] = f"--config={musl_cfg} -pipe -D_FILE_OFFSET_BITS=64"
            os.environ["CXXFLAGS"] = f"--config={musl_cxx_cfg} -pipe -D_FILE_OFFSET_BITS=64"
            os.environ["CFLAGS_STATIC"] = f"--config={musl_static_cfg} -pipe -D_FILE_OFFSET_BITS=64"

            all_target_repos = [r for r in repos if r["type"] in ["base", "other"]]

            print(f"[{arch}] Target Phase 0: System Headers (musl & linux-headers)")
            for name in ["musl", "linux-headers"]:
                proj = next((r for r in all_target_repos if r["name"] == name), None)
                if proj and ((not args.build) or args.build == "ALL" or args.build == proj["name"]):
                    module = load_repo_una(proj["repo_dir"], proj.get("una_file", "una.py"))
                    kwargs = {"arch": arch}
                    if proj["name"] == "linux-headers":
                        kconfig = args.kconfig or BASE_DIR / "confs" / f"kernel.{arch}.config"
                        kwargs["kconfig"] = Path(kconfig).absolute()

                    if hasattr(module, "target_configure"): runner.run_step(proj, "target_configure", module.target_configure, **kwargs)
                    if hasattr(module, "target_headers_install"): runner.run_step(proj, "target_headers_install", module.target_headers_install, **kwargs)

            print(f"[{arch}] Target Phase 1: Core Base Library (musl)")
            musl_proj = next((r for r in all_target_repos if r["name"] == "musl"), None)
            if musl_proj and ((not args.build) or args.build == "ALL" or args.build == musl_proj["name"]):
                module = load_repo_una(musl_proj["repo_dir"], musl_proj.get("una_file", "una.py"))
                if hasattr(module, "target_build"): runner.run_step(musl_proj, "target_build", module.target_build, arch=arch)
                if hasattr(module, "target_install"): runner.run_step(musl_proj, "target_install", module.target_install, arch=arch)

            base_repos = [
                   r for r in target_configs_to_build
                   if r["type"] == "base" and r["name"] not in ("musl", "linux-headers")
            ]
            if base_repos:
                print(f"[{arch}] Target Phase 2: Base Components")
                for r in base_repos:
                    if r and ((not args.build) or args.build == "ALL" or args.build == r["name"]):
                        module = load_repo_una(r["repo_dir"], r.get("una_file", "una.py"))
                        print(f"[{arch}] [{module}]")
                        if hasattr(module, "target_configure"): runner.run_step(r, "target_configure", module.target_configure, arch=arch)
                        if hasattr(module, "target_headers_install"): runner.run_step(r, "target_headers_install", module.target_headers_install, arch=arch)
                        if hasattr(module, "target_build"): runner.run_step(r, "target_build", module.target_build, arch=arch)
                        if hasattr(module, "target_install"): runner.run_step(r, "target_install", module.target_install, arch=arch)

            print(f"[{arch}] Target Phase 3: Other Components")
            other_repos = [r for r in target_configs_to_build if r["type"] == "other" and r["name"] != "linux-image"]
            if other_repos:
                for r in other_repos:
                    if r and ((not args.build) or args.build == "ALL" or args.build == r["name"]):
                        module = load_repo_una(r["repo_dir"], r.get("una_file", "una.py"))
                        if hasattr(module, "target_configure"): runner.run_step(r, "target_configure", module.target_configure, arch=arch)
                        if hasattr(module, "target_headers_install"): runner.run_step(r, "target_headers_install", module.target_headers_install, arch=arch)
                        if hasattr(module, "target_build"): runner.run_step(r, "target_build", module.target_build, arch=arch)
                        if hasattr(module, "target_install"): runner.run_step(r, "target_install", module.target_install, arch=arch)

            # Ensure skel/etc overrides anything installed by components before kernel packing
            skel_etc = skel_dir / "etc"
            if skel_etc.exists():
                print(f"[{arch}] Finalizing: Replacing /etc with skel/etc before kernel build...")
                shutil.rmtree(staging_dir / "etc", ignore_errors=True)
                shutil.rmtree(target_dir / "etc", ignore_errors=True)
                shutil.copytree(skel_etc, staging_dir / "etc", symlinks=True)
                shutil.copytree(skel_etc, target_dir / "etc", symlinks=True)

            linux_proj = next((r for r in target_configs_to_build if r["name"] == "linux-image"), None)
            if linux_proj:
                print(f"[{arch}] Target Phase 4: Kernel Finalization")
                module = load_repo_una(linux_proj["repo_dir"], linux_proj.get("una_file", "una.py"))
                
                kconfig = args.kconfig or BASE_DIR / "confs" / f"kernel.{arch}.config"
                kconfig_path = Path(kconfig).absolute()
                
                if hasattr(module, "target_configure"): runner.run_step(r, "target_configure", module.target_configure, arch=arch, kconfig=kconfig_path)
                if hasattr(module, "target_build"): runner.run_step(linux_proj, "target_build", module.target_build, arch=arch, kconfig=kconfig_path)
                if hasattr(module, "target_install"): runner.run_step(linux_proj, "target_install", module.target_install, arch=arch, kconfig=kconfig_path)

                # Export kernel image
                if "kernel_image" in linux_proj:
                    image_map = linux_proj["kernel_image"]
                    if arch in image_map:
                        rel_path = image_map[arch]
                        src_img = Path(linux_proj["repo_dir"]) / rel_path
                        dest_img = bld_base / f"kernel.{arch}"
                        if src_img.exists():
                            print(f"[{arch}] Copying kernel image to {dest_img}")
                            shutil.copy(src_img, dest_img)
                        else:
                            print(f"[{arch}] Warning: Kernel image not found at {src_img}")
                        
                        # Sync back updated config to source
                        src_config = Path(linux_proj["repo_dir"]) / ".config"
                        if src_config.exists():
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
        
        kernel_img = bld_base / f"kernel.{arch}"
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
            print(f"\n--- Top-level Repository (una) ---")
            top_repo = Repo(top_repo_path)
            # For top-level, we assume 'una' branch rebasing onto 'una/una'
            target_branch = "una/una" 
            if tag:
                save_and_push(top_repo, target_branch, tag, remote_name="una")
            elif args.rebase:
                rebase_and_push(top_repo, target_branch, remote_name="una")
            processed_dirs.add(top_repo_path)

        # Handle sub-repositories
        for cfg in repos_to_process:
            r_path = Path(cfg["repo_dir"]).absolute()
            if r_path in processed_dirs: continue
            
            print(f"\n--- Repository: {cfg['name']} ({cfg['repo_dir']}) ---")
            repo = Repo(r_path)
            remote_prefix = "origin" if args.init_with_origin else "una"
            if remote_prefix == "una":
                target_branch = "una/una"
            else:
                target_branch = f"origin/{cfg.get('branch', 'master')}"
            
            if tag:
                save_and_push(repo, target_branch, tag)
            elif args.rebase:
                rebase_and_push(repo, target_branch)
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
                
        # 3. Clean top-level workspace (excluding reports, kernel images, and repos)
        print("Cleaning top-level workspace...")
        if is_repo_dirty(BASE_DIR):
            print("ERROR: Top-level repository is dirty. Stopping global cleanup.")
            sys.exit(1)
        subprocess.run(["git", "clean", "-xfd", "-e", "bld/", "-e", "repo/"], cwd=BASE_DIR)


if __name__ == "__main__":
    main()
