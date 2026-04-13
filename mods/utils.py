from git import Repo, RemoteProgress
import shutil
import os
import sys
from tqdm import tqdm

default_branch = "una"
remote_una_name = "una"


class TqdmProgress(RemoteProgress):
    def __init__(self):
        super().__init__()
        self.pbar = tqdm(
            desc="Git operation",
            unit="obj",
            leave=False,
            dynamic_ncols=True,
        )

    def update(self, op_code, cur_count, max_count=None, message=""):
        if max_count is not None:
            self.pbar.total = max_count

        self.pbar.n = cur_count

        if message:
            self.pbar.set_description(message, refresh=False)

        self.pbar.refresh()

    def __del__(self):
        if hasattr(self, "pbar") and self.pbar is not None:
            try:
                self.pbar.close()
            except Exception:
                pass


def init_or_reset_repo(repo_dir: str, origin_url: str, una_url: str) -> Repo:
    print(f"Initializing repo: {repo_dir}")
    if not os.path.exists(repo_dir):
        print(f"Cloning repo into {repo_dir}...")
        repo = Repo.clone_from(origin_url, repo_dir, progress=TqdmProgress())
    else:
        print(f"Repo exists at {repo_dir}; opening...")
        repo = Repo(repo_dir)

    print("Configuring SSH key...")
    repo.git.config("core.sshCommand", "ssh -i ~/.github.key -o IdentitiesOnly=yes")

    if "origin" not in [r.name for r in repo.remotes]:
        repo.create_remote("origin", origin_url)
    else:
        repo.remotes.origin.set_url(origin_url)

    if remote_una_name not in [r.name for r in repo.remotes]:
        repo.create_remote(remote_una_name, una_url)
    else:
        repo.remotes[remote_una_name].set_url(una_url)

    print("Setting fetch refspec for origin...")
    repo.git.config("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")

    print("Setting fetch refspec for una...")
    repo.git.config(f"remote.{remote_una_name}.fetch", f"+refs/heads/*:refs/remotes/{remote_una_name}/*")

    print("Cleaning up stale una refs...")
    una_refs_path = os.path.join(repo.git_dir, f"refs/remotes/{remote_una_name}")
    if os.path.exists(una_refs_path):
        shutil.rmtree(una_refs_path, ignore_errors=True)

    print("Fetching latest changes from origin...")
    repo.remotes.origin.fetch(progress=TqdmProgress())

    print("Fetching latest changes from una...")
    repo.remotes.una.fetch(progress=TqdmProgress())

    print("Checking out branch una...")
    try:
        remote_ref = repo.remotes.una.refs[default_branch]
    except (IndexError, AttributeError):
        print(f"Error: Branch '{default_branch}' not found on remote '{remote_una_name}'.")
        sys.exit(1)

    if default_branch in repo.heads:
        repo.heads[default_branch].set_tracking_branch(remote_ref)
        repo.heads[default_branch].checkout()
    else:
        local_branch = repo.create_head(default_branch, remote_ref)
        local_branch.set_tracking_branch(remote_ref)
        local_branch.checkout()

    print("Running git clean -fdx...")
    repo.git.clean("-fdx")

    print("Running git reset --hard...")
    repo.head.reset(index=True, working_tree=True)

    return repo


def rebase_and_push(repo: Repo, branch_name: str):
    print(f"Rebasing current branch upon {branch_name}...")
    # These will raise exceptions on failure, which will stop the script
    repo.git.rebase(branch_name)

    print("Creating automatic rebase commit...")
    # allow-empty to ensure we always have the 'rebase' marker if requested
    repo.git.commit("--allow-empty", "-m", "rebase")

    print(f"Pushing rebased branch to {remote_una_name}...")
    repo.remotes[remote_una_name].push(repo.active_branch.name, force=True, progress=TqdmProgress())


def get_target_triple(arch: str) -> str:
    if arch == "x32":
        return "x86_64-linux-muslx32"
    elif arch == "x86_64":
        return "x86_64-linux-musl"
    elif arch == "aarch64":
        return "aarch64-linux-musl"
    elif arch == "riscv64":
        return "riscv64-linux-musl"
    else:
        raise ValueError(f"Unsupported architecture: {arch}")


def get_arch_flags(arch: str) -> str:
    if arch == "x32":
        return "-mx32"
    elif arch == "x86_64":
        return "-m64"
    elif arch == "aarch64":
        return "-march=armv8-a"
    elif arch == "riscv64":
        return "-march=rv64gc -mabi=lp64d"
    else:
        return "-O2"


def get_kernel_arch(arch: str) -> str:
    return {
        "x32": "x86",
        "x86_64": "x86",
        "aarch64": "arm64",
        "riscv64": "riscv"
    }.get(arch, "x86")


def get_cross_prefix(arch: str) -> str:
    if arch == "x32":
        return "x86_64-pc-linux-muslx32-"
    elif arch == "x86_64":
        return "x86_64-pc-linux-musl-"
    elif arch == "aarch64":
        return "aarch64-linux-musl-"
    elif arch == "riscv64":
        return "riscv64-linux-musl-"
    else:
        return f"{arch}-linux-musl-"