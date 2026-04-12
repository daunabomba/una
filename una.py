#!/usr/bin/python

import argparse
import shutil
import os

from mods.utils import init_or_reset_repo, rebase_and_push
from pathlib import Path

bld_base = Path("./bld").absolute()
skel_dir = Path("./skel").absolute()

import importlib.util
import sys


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
    """
    try:
        from git import Repo
        repo = Repo(Path(__file__).parent, search_parent_directories=True)
        url = repo.remotes.origin.url
        if "/" in url:
            return url.rsplit("/", 1)[0]
    except Exception:
        pass
    return None


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
        help="Rebase the local 'una' branch onto its configured upstream origin branch and push to una.",
    )
    parser.add_argument(
        "--arch",
        choices=["x32", "x86_64"],
        default="x32",
        help="Target architecture (default: x32)",
    )

    args = parser.parse_args()
    
    arch = args.arch
    host_install_dir = bld_base / "host"
    arch_bld_dir = bld_base / arch
    staging_dir = arch_bld_dir / "staging"
    target_dir = arch_bld_dir / "target"

    if args.init == "DETECT_FAILED":
        print("Error: --init was used without a BASE_URL, and no git remote origin was detected.")
        sys.exit(1)

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    # Configuration for components and repositories
    repos_config = [
        {
            "name": "llvm-host",
            "una_repo": "llvm-project.git",
            "repo_dir": "./repo/llvm",
            "una_file": "una/host.py",
            "origin_url": "/mnt/work/bld/llvm-project.git",
            "type": "host",
            "branch": "main",
            "rebase": True,
        },
        {
            "name": "musl",
            "una_repo": "musl.git",
            "repo_dir": "./repo/musl",
            "origin_url": "/mnt/work/bld/musl.git",
            "type": "base",
            "branch": "master",
            "rebase": True,
        },
        {
            "name": "llvm-runtime",
            "una_repo": "llvm-project.git",
            "repo_dir": "./repo/llvm",
            "una_file": "una/runtime.py",
            "origin_url": "/mnt/work/bld/llvm-project.git",
            "type": "base",
            "branch": "main",
            "rebase": True,
        },
        {
            "name": "busybox",
            "una_repo": "busybox.git",
            "repo_dir": "./repo/busybox",
            "origin_url": "/mnt/work/bld/busybox.git",
            "type": "other",
            "branch": "master",
            "rebase": True,
        },
        {
            "name": "openssl",
            "una_repo": "openssl.git",
            "repo_dir": "./repo/openssl",
            "origin_url": "/mnt/work/bld/openssl.git",
            "type": "other",
            "branch": "master",
            "rebase": True,
        },
        {
            "name": "mxmux",
            "una_repo": "mxmux.git",
            "repo_dir": "./repo/mxmux",
            "origin_url": "https://github.com/daunabomba/mxmux.git",
            "type": "other",
            "branch": "master",
            "rebase": False,
        },
        {
            "name": "wireguard-tools",
            "una_repo": "wireguard-tools.git",
            "repo_dir": "./repo/wireguard-tools",
            "origin_url": "https://git.zx2c4.com/wireguard-tools",
            "type": "other",
            "branch": "master",
            "rebase": True,
        },
        {
            "name": "linux",
            "una_repo": "linux.git",
            "repo_dir": "./repo/kernel",
            "origin_url": "/mnt/work/bld/linux-stable.git",
            "type": "other",
            "branch": "master",
            "rebase": True,
        },
    ]

    repos = []
    una_base = args.init
    
    for r in repos_config:
        config = r.copy()
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

    if args.init:
        if args.build == "ALL" or not args.build:
            # We don't wipe everything, only the current arch's target directories
            # Host tools are kept unless specifically requested or first build
            shutil.rmtree(arch_bld_dir, ignore_errors=True)
            host_install_dir.mkdir(parents=True, exist_ok=True)
            staging_dir.mkdir(parents=True, exist_ok=True)
            target_dir.mkdir(parents=True, exist_ok=True)
 
            if skel_dir.exists():
                print(f"Propagating skel contents to {arch} staging and target directories...")
                shutil.copytree(skel_dir, staging_dir, symlinks=True, dirs_exist_ok=True)
                shutil.copytree(skel_dir, target_dir, symlinks=True, dirs_exist_ok=True)
            
        initialized_dirs = set()
        for cfg in repos_to_process:
            repo_dir = cfg["repo_dir"]
            if repo_dir in initialized_dirs:
                continue
            init_or_reset_repo(repo_dir=repo_dir, origin_url=cfg["origin_url"], una_url=cfg["una_url"])
            initialized_dirs.add(repo_dir)

    if args.build:
        print("Starting build process.")
        
        host_repos = [r for r in repos_to_process if r["type"] == "host"]
        if host_repos:
            print("\n--- Host Stage ---")
            for r in host_repos:
                module = load_repo_una(r["repo_dir"], r.get("una_file", "una.py"))
                if hasattr(module, "host_configure"): module.host_configure(host_install_dir)
                if hasattr(module, "host_build"): module.host_build(host_install_dir)
                if hasattr(module, "host_install"): module.host_install(host_install_dir)

        target_configs_to_build = [r for r in repos_to_process if r["type"] in ["base", "other"]]
        if target_configs_to_build:
            print(f"\n--- Target Stage ({args.arch}) ---")
            
            arch = args.arch
            if arch == "x32":
                target_triple = "x86_64-linux-muslx32"
                march = "-mx32"
                ld_musl = "/usr/lib/ld-musl-x32.so.1"
            else:
                target_triple = "x86_64-linux-musl"
                march = "-m64"
                ld_musl = "/usr/lib/ld-musl-x86_64.so.1"

            musl_cfg = arch_bld_dir / "musl.cfg"
            musl_cpp_cfg = arch_bld_dir / "muslc++.cfg"
            musl_static_cfg = arch_bld_dir / "musl_static.cfg"
            
            if not musl_cfg.exists() or not musl_cpp_cfg.exists() or not musl_static_cfg.exists():
                print(f"Generating compiler configurations for {arch}...")
                lld_path = host_install_dir / "bin" / "ld.lld"
                lib_p = staging_dir / "usr" / "lib"
                # Pure C Config
                musl_cfg.write_text(f"--target={target_triple}\n--sysroot={staging_dir}\n-isystem {staging_dir}/usr/include\n-fuse-ld={lld_path}\n-nostdlib\n-L{staging_dir}/usr/lib\n-lc\n-Wl,-dynamic-linker,{ld_musl}\n-fPIE\n{march}\n")
                # C++ Config
                musl_cpp_cfg.write_text(f"--target={target_triple}\n--sysroot={staging_dir}\n-isystem {staging_dir}/usr/include/c++/v1\n-isystem {staging_dir}/usr/include\n--ld-path={lld_path}\n-nostdlib\n{lib_p}/Scrt1.o\n{lib_p}/crti.o\n-L{lib_p}\n-lc++\n-lc++abi\n-lunwind\n-lc\n{lib_p}/crtn.o\n-Wl,-dynamic-linker,{ld_musl}\n-fPIE\n{march}\n")
                musl_static_cfg.write_text(f"--target={target_triple}\n--sysroot={staging_dir}\n-isystem {staging_dir}/usr/include\n-fuse-ld={lld_path}\n-nostdlib\n{lib_p}/Scrt1.o\n{lib_p}/crti.o\n-L{lib_p}\n-lc\n{lib_p}/crtn.o\n-Wl,-dynamic-linker,{ld_musl}\n-fPIE\n{march}\n")

            os.environ["CFLAGS"] = f"--config={musl_cfg} -pipe -D_FILE_OFFSET_BITS=64"
            os.environ["CXXFLAGS"] = f"--config={musl_cpp_cfg} -pipe -D_FILE_OFFSET_BITS=64"
            os.environ["CFLAGS_STATIC"] = f"--config={musl_static_cfg} -pipe -D_FILE_OFFSET_BITS=64"

            all_target_repos = [r for r in repos if r["type"] in ["base", "other"]]

            print("Target Phase 0: System Headers (musl & linux)")
            for name in ["musl", "linux"]:
                proj = next((r for r in all_target_repos if r["name"] == name), None)
                if proj:
                    module = load_repo_una(proj["repo_dir"], proj.get("una_file", "una.py"))
                    if hasattr(module, "target_configure"): module.target_configure(staging_dir, target_dir, arch=args.arch)
                    if hasattr(module, "target_headers_install"): module.target_headers_install(staging_dir, target_dir, arch=args.arch)

            print("Target Phase 1: Core Base Library (musl)")
            musl_proj = next((r for r in all_target_repos if r["name"] == "musl"), None)
            if musl_proj:
                module = load_repo_una(musl_proj["repo_dir"], musl_proj.get("una_file", "una.py"))
                if hasattr(module, "target_build"): module.target_build(staging_dir, target_dir, arch=args.arch)
                if hasattr(module, "target_install"): module.target_install(staging_dir, target_dir, arch=args.arch)

            base_repos = [r for r in target_configs_to_build if r["type"] == "base" and r["name"] != "musl"]
            if base_repos:
                print("Target Phase 2: Base Components")
                for r in base_repos:
                    module = load_repo_una(r["repo_dir"], r.get("una_file", "una.py"))
                    if hasattr(module, "target_configure"): module.target_configure(staging_dir, target_dir, arch=args.arch)
                    if hasattr(module, "target_headers_install"): module.target_headers_install(staging_dir, target_dir, arch=args.arch)
                    if hasattr(module, "target_build"): module.target_build(staging_dir, target_dir, arch=args.arch)
                    if hasattr(module, "target_install"): module.target_install(staging_dir, target_dir, arch=args.arch)

            other_repos = [r for r in target_configs_to_build if r["type"] == "other" and r["name"] != "linux"]
            if other_repos:
                print("Target Phase 3: Other Components")
                for r in other_repos:
                    module = load_repo_una(r["repo_dir"], r.get("una_file", "una.py"))
                    if hasattr(module, "target_configure"): module.target_configure(staging_dir, target_dir, arch=args.arch)
                    if hasattr(module, "target_headers_install"): module.target_headers_install(staging_dir, target_dir, arch=args.arch)
                    if hasattr(module, "target_build"): module.target_build(staging_dir, target_dir, arch=args.arch)
                    if hasattr(module, "target_install"): module.target_install(staging_dir, target_dir, arch=args.arch)

            # Ensure skel/etc overrides anything installed by components before kernel packing
            skel_etc = skel_dir / "etc"
            if skel_etc.exists():
                print("Finalizing: Replacing /etc with skel/etc before kernel build...")
                shutil.rmtree(staging_dir / "etc", ignore_errors=True)
                shutil.rmtree(target_dir / "etc", ignore_errors=True)
                shutil.copytree(skel_etc, staging_dir / "etc", symlinks=True)
                shutil.copytree(skel_etc, target_dir / "etc", symlinks=True)

            linux_proj = next((r for r in target_configs_to_build if r["name"] == "linux"), None)
            if linux_proj:
                print("Target Phase 4: Kernel Finalization")
                module = load_repo_una(linux_proj["repo_dir"], linux_proj.get("una_file", "una.py"))
                if hasattr(module, "target_build"): module.target_build(staging_dir, target_dir, arch=args.arch)
                if hasattr(module, "target_install"): module.target_install(staging_dir, target_dir, arch=args.arch)

    if args.rebase:
        from git import Repo
        rebased_dirs = set()
        for cfg in repos_to_process:
            if not cfg.get("rebase", False): continue
            rebase_and_push(Repo(cfg["repo_dir"]), f"origin/{cfg.get('branch', 'master')}")
            rebased_dirs.add(cfg["repo_dir"])


if __name__ == "__main__":
    main()
