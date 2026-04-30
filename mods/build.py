"""
Build logic for una - extracted to avoid indentation issues.
"""

import sys
import os
import subprocess
import json
import shutil
from pathlib import Path

from mods.trace import (
    is_enabled,
    tools_step_start,
    tools_step_end,
    build_step_start,
    build_step_end,
)

# These will be set by init_build()
colors = None
load_repo_una = None
StepRunner = None
get_target_triple = None
get_arch_flags = None
propagate_skel = None
sync_kernel_config = None
is_repo_dirty = None
BASE_DIR = None
bld_base = None
arches = None
repos = None
repos_to_process = None
required_names = None
build_all = None
tools_install_dir = None
skel_dir = None
global_cfg = None


def init_build(
    colors_mod,
    load_repo_una_func,
    StepRunner_class,
    get_target_triple_func,
    get_arch_flags_func,
    propagate_skel_func,
    sync_kernel_config_func,
    is_repo_dirty_func,
    BASE_DIR_val,
    bld_base_val,
    arches_val,
    repos_val,
    repos_to_process_val,
    required_names_val,
    build_all_val,
    tools_install_dir_val,
    skel_dir_val,
    global_cfg_val,
):
    """Initialize the build module with required functions and variables."""
    global colors, load_repo_una, StepRunner, get_target_triple
    global get_arch_flags, propagate_skel, sync_kernel_config, is_repo_dirty
    global BASE_DIR, bld_base, arches, repos, repos_to_process
    global required_names, build_all, tools_install_dir, skel_dir, global_cfg

    colors = colors_mod
    load_repo_una = load_repo_una_func
    StepRunner = StepRunner_class
    get_target_triple = get_target_triple_func
    get_arch_flags = get_arch_flags_func
    propagate_skel = propagate_skel_func
    sync_kernel_config = sync_kernel_config_func
    is_repo_dirty = is_repo_dirty_func
    BASE_DIR = BASE_DIR_val
    bld_base = bld_base_val
    arches = arches_val
    repos = repos_val
    repos_to_process = repos_to_process_val
    required_names = required_names_val
    build_all = build_all_val
    tools_install_dir = tools_install_dir_val
    skel_dir = skel_dir_val
    global_cfg = global_cfg_val


def run_build(args):
    """Main build function."""
    colors.info("Starting build process.")
    all_possible_arches = ["x32", "x86_64", "aarch64", "riscv64"]
    tools_state_file = BASE_DIR / "bld" / "tools" / "tools_state"

    def get_repo_commit(repo_path):
        if not (repo_path / ".git").exists():
            return None
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_path, capture_output=True, text=True
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def load_tools_state():
        if not tools_state_file.exists():
            return {}
        try:
            with open(tools_state_file, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_tools_state(state):
        tools_state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(tools_state_file, "w") as f:
            json.dump(state, f, indent=2)

    def check_tools_changed(tools_repos):
        current_state = {
            r["name"]: get_repo_commit(Path(r["repo_dir"])) for r in tools_repos
        }
        saved_state = load_tools_state()

        for name, commit in current_state.items():
            if commit is None:
                continue
            if name not in saved_state or saved_state[name] != commit:
                return True
        return False

    all_tools_repos = [r for r in repos if r.get("type") == "tools"]

    # Also check if any component explicitly requested tools (like build-tools)
    has_tool_component = "build-tools" in required_names

    tools_to_build = []
    if build_all:
        # Check if tools need rebuilding
        if not tools_state_file.exists():
            colors.info("Tools not built, building...")
            tools_to_build = all_tools_repos
        elif check_tools_changed(all_tools_repos):
            colors.info("Tools source changed, will rebuild...")
            tools_to_build = all_tools_repos
        else:
            colors.info("Tools already built and up to date, skipping...")
    elif repos_to_process:
        target_requires_tools = any(r.get("type") != "tools" for r in repos_to_process)
        if target_requires_tools:
            if not tools_state_file.exists():
                colors.info("Tools not built, building...")
                tools_to_build = all_tools_repos
            elif check_tools_changed(all_tools_repos):
                colors.info("Tools source changed, will rebuild...")
                tools_to_build = all_tools_repos
            else:
                colors.info("Tools already built and up to date, skipping...")
    else:
        # No specific target - this is a default --build without args
        if not tools_state_file.exists():
            colors.info("Tools not built, building...")
            tools_to_build = all_tools_repos
        elif check_tools_changed(all_tools_repos):
            colors.info("Tools source changed, will rebuild...")
            tools_to_build = all_tools_repos
        else:
            colors.info("Tools already built and up to date, skipping...")

    if tools_to_build:
        colors.info("\n--- Tools Stage ---")
        if is_enabled():
            tools_step_start("tools_configure")
        for r in tools_to_build:
            module = load_repo_una(r["repo_dir"], r.get("una_file", "una.py"))
            if hasattr(module, "tools_configure"):
                module.tools_configure(tools_install_dir, arches=all_possible_arches)
            if hasattr(module, "tools_build"):
                module.tools_build(tools_install_dir)
            if hasattr(module, "tools_install"):
                module.tools_install(tools_install_dir)
        if is_enabled():
            tools_step_end("tools_configure")
        new_state = {
            r["name"]: get_repo_commit(Path(r["repo_dir"])) for r in tools_to_build
        }
        save_tools_state(new_state)

    target_configs_to_build = [
        r
        for r in repos_to_process
        if r.get("type") != "tools"
        and not r.get("is_virtual")
        and r.get("type") != "virtual"
        and "repo_dir" in r
    ]
    for arch in arches:
        colors.info(f"\n====== Target Stage: {arch} ======")
        arch_bld_dir = bld_base / arch
        staging_dir = arch_bld_dir / "staging"
        target_dir = arch_bld_dir / "target"
        runner = StepRunner(arch, staging_dir, target_dir, bld_base)

        # Ensure build directories exist and skel is propagated
        staging_dir.mkdir(parents=True, exist_ok=True)
        target_dir.mkdir(parents=True, exist_ok=True)
        if skel_dir.exists():
            colors.info(f"[{arch}] Phase -1: Skeleton Propagation (verified)")
            skel_runner = StepRunner(arch, staging_dir, target_dir, bld_base)
            skel_runner.run_step(
                cfg={"name": "skel"}, step_name="propagate", step_func=propagate_skel
            )
        else:
            colors.warn(f"[{arch}] No skel directory found - empty staging/target")

        # Clean ALL target repos before build to avoid stale configs/artifacts between arches
        cleaned_dirs = set()
        tools_dirs = {
            Path(r["repo_dir"]).absolute()
            for r in repos
            if r.get("type") == "tools" and "repo_dir" in r
        }
        all_target_repos = [
            r for r in repos if r.get("type") != "tools" and "repo_dir" in r
        ]

        for r in all_target_repos:
            r_path = Path(r["repo_dir"]).absolute()
            if r_path in cleaned_dirs:
                continue
            if r_path in tools_dirs:
                colors.info(
                    f"[{arch}] Skipping git clean for {r['name']} (shared with tools components)"
                )
                continue
            if not r_path.exists():
                continue
            colors.info(f"[{arch}] Cleaning {r['name']} ({r_path})...")
            if is_repo_dirty(r_path):
                colors.error(
                    f"[{arch}] ERROR: Repository {r['name']} is dirty. Please commit or stash changes before building."
                )
                sys.exit(1)
            subprocess.run(
                ["git", "clean", "-fdx", "-e", ".una_config"], cwd=r_path, check=True
            )
            # Ensure submodules are also cleaned
            if (r_path / ".gitmodules").exists():
                try:
                    subprocess.run(
                        [
                            "git",
                            "submodule",
                            "foreach",
                            "--recursive",
                            "git",
                            "clean",
                            "-fdx",
                            "-e",
                            ".una_config",
                        ],
                        cwd=r_path,
                        check=True,
                    )
                except subprocess.CalledProcessError:
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
        musl_cxx_cfg = arch_bld_dir / "musl_cxx.cfg"
        musl_static_cfg = arch_bld_dir / "musl_static.cfg"

        if (
            not musl_cfg.exists()
            or not musl_cxx_cfg.exists()
            or not musl_static_cfg.exists()
        ):
            colors.info(f"[{arch}] Generating compiler configurations...")
            arch_bld_dir.mkdir(parents=True, exist_ok=True)
            lld_path = tools_install_dir / "bin" / "ld.lld"
            lib_p = staging_dir / "usr" / "lib"

            # Common flags
            common_flags = (
                f"--target={target_triple}\n--sysroot={staging_dir}\n-fPIE\n{march}\n"
            )

            # Pure C Config
            musl_cfg.write_text(
                f"{common_flags}-isystem {staging_dir}/usr/include\n-fuse-ld={lld_path}\n-nostdlib\n-L{staging_dir}/usr/lib\n-lc\n-Wl,-dynamic-linker,{ld_musl}\n"
            )

            # C++ Config
            musl_cxx_cfg.write_text(
                f"{common_flags}-isystem {staging_dir}/usr/include/c++/v1\n-isystem {staging_dir}/usr/include\n--ld-path={lld_path}\n-nostdlib\n{lib_p}/Scrt1.o\n{lib_p}/crti.o\n-L{lib_p}\n-lc++\n-lc++abi\n-lunwind\n-lc\n{lib_p}/crtn.o\n-Wl,-dynamic-linker,{ld_musl}\n"
            )

            # Static Config
            musl_static_cfg.write_text(
                f"{common_flags}-isystem {staging_dir}/usr/include\n-fuse-ld={lld_path}\n-nostdlib\n{lib_p}/Scrt1.o\n{lib_p}/crti.o\n-L{lib_p}\n-lc\n{lib_p}/crtn.o\n-Wl,-dynamic-linker,{ld_musl}\n"
            )

        cpu_flags = global_cfg.get("cpu_flags", "")
        os.environ["CFLAGS"] = (
            f"--config={musl_cfg} -pipe -D_FILE_OFFSET_BITS=64 {cpu_flags}"
        )
        os.environ["CXXFLAGS"] = (
            f"--config={musl_cxx_cfg} -pipe -D_FILE_OFFSET_BITS=64 {cpu_flags}"
        )
        os.environ["CFLAGS_STATIC"] = (
            f"--config={musl_static_cfg} -pipe -D_FILE_OFFSET_BITS=64 {cpu_flags}"
        )
        os.environ["CPPFLAGS"] = f"-D_FILE_OFFSET_BITS=64 {cpu_flags}"

        colors.info(f"[{arch}] Building Target Components in Dependency Order")
        for r in target_configs_to_build:
            if r.get("is_virtual") or r.get("type") == "virtual":
                colors.info(f"[{arch}] Skipping virtual component '{r['name']}'")
                continue

            colors.info(f"[{arch}] Processing component: {r['name']}")

            if r["name"] == "linux-image":
                skel_etc = None
                if "etc_dir" in global_cfg:
                    skel_etc = BASE_DIR / global_cfg["etc_dir"]
                else:
                    skel_etc = skel_dir / "etc"

                if skel_etc.exists():
                    colors.info(
                        f"[{arch}] Finalizing: Replacing /etc with {skel_etc} before kernel build..."
                    )
                    shutil.rmtree(staging_dir / "etc", ignore_errors=True)
                    shutil.rmtree(target_dir / "etc", ignore_errors=True)
                    shutil.copytree(skel_etc, staging_dir / "etc", symlinks=True)
                    shutil.copytree(skel_etc, target_dir / "etc", symlinks=True)
                elif "etc_dir" in global_cfg:
                    colors.error(
                        f"[{arch}] Error: Skel etc override path {skel_etc} does not exist."
                    )
                    sys.exit(1)

            colors.info(f"[{arch}] DEBUG: Building repo '{r['name']}'")
            colors.info(f"[{arch}] DEBUG: Repo path: {r['repo_dir']}")
            una_file = r.get("una_file", "una.py")
            colors.info(f"[{arch}] DEBUG: Loading module '{una_file}'")
            module = load_repo_una(r["repo_dir"], una_file)
            colors.info(f"[{arch}] DEBUG: Loaded module name: {module.__name__}")
            kwargs = {"arch": arch}

            if r["name"] in ["linux-headers", "linux-image"]:
                kconfig = None
                if "kconfig" in global_cfg:
                    kconfig = BASE_DIR / global_cfg["kconfig"].replace("<arch>", arch)
                if not kconfig:
                    kconfig = BASE_DIR / "confs" / f"kernel.{arch}.config"
                kwargs["kconfig"] = Path(kconfig).absolute()

            if hasattr(module, "target_configure"):
                if is_enabled():
                    build_step_start(arch, r["name"], "target_configure")
                runner.run_step(
                    r, "target_configure", module.target_configure, **kwargs
                )
                if is_enabled():
                    build_step_end(arch, r["name"], "target_configure")
            if hasattr(module, "target_headers_install"):
                if is_enabled():
                    build_step_start(arch, r["name"], "target_headers_install")
                runner.run_step(
                    r, "target_headers_install", module.target_headers_install, **kwargs
                )
                if is_enabled():
                    build_step_end(arch, r["name"], "target_headers_install")
            if hasattr(module, "target_build"):
                if is_enabled():
                    build_step_start(arch, r["name"], "target_build")
                runner.run_step(r, "target_build", module.target_build, **kwargs)
                if is_enabled():
                    build_step_end(arch, r["name"], "target_build")
            if hasattr(module, "target_install"):
                if is_enabled():
                    build_step_start(arch, r["name"], "target_install")
                runner.run_step(r, "target_install", module.target_install, **kwargs)
                if is_enabled():
                    build_step_end(arch, r["name"], "target_install")

            if r["name"] == "linux-image" and "kernel_image" in r:
                image_map = r["kernel_image"]
                if arch in image_map:
                    rel_path = image_map[arch]
                    src_img = Path(r["repo_dir"]) / rel_path

                    kernel_name = global_cfg.get("kernel_name", "kernel")
                    dest_img = bld_base / kernel_name

                    if src_img.exists():
                        print(f"[{arch}] Copying kernel image to {dest_img}")
                        shutil.copy(src_img, dest_img)
                    else:
                        print(f"[{arch}] Warning: Kernel image not found at {src_img}")

                    # Copy initfilelist with .txt extension
                    src_initfilelist = Path(r["repo_dir"]) / "initfilelist"
                    dest_initfilelist = bld_base / f"{kernel_name}.list"
                    if src_initfilelist.exists():
                        print(f"[{arch}] Copying initfilelist to {dest_initfilelist}")
                        shutil.copy(src_initfilelist, dest_initfilelist)
                    else:
                        print(f"[{arch}] Warning: initfilelist not found at {src_initfilelist}")

                    # Sync back updated config to source
                    src_config = Path(r["repo_dir"]) / ".config"
                    if src_config.exists():
                        kconfig_path = kwargs.get("kconfig")
                        print(
                            f"[{arch}] Syncing back sanitized updated kernel config to {kconfig_path}"
                        )
                        sync_kernel_config(src_config, kconfig_path)
                else:
                    print(
                        f"[{arch}] Warning: No kernel image path defined for this architecture"
                    )

    # Post-build cleanup for workspace repositories
    print("\n--- Post-build Workspace Cleanup ---")
    cleaned_dirs = set()
    for r in repos:
        # Skip virtual components
        if r.get("is_virtual") or r.get("type") == "virtual" or "repo_dir" not in r:
            continue
        r_path = Path(r["repo_dir"]).absolute()
        if r_path in cleaned_dirs:
            continue
        if r_path.exists() and (r_path / ".git").exists():
            print(f"Cleaning {r['name']} ({r['repo_dir']})...")
            if is_repo_dirty(r_path):
                print(
                    f"ERROR: Repository {r['name']} is dirty. Skipping post-build cleanup for this repo."
                )
                continue
            subprocess.run(["git", "clean", "-qfdx"], cwd=r_path, check=True)
            if (r_path / ".gitmodules").exists():
                try:
                    subprocess.run(
                        [
                            "git",
                            "submodule",
                            "foreach",
                            "--recursive",
                            "git",
                            "clean",
                            "-qfdx",
                        ],
                        cwd=r_path,
                        check=True,
                    )
                except subprocess.CalledProcessError:
                    pass
            cleaned_dirs.add(r_path)

    # End of build process
    return True
