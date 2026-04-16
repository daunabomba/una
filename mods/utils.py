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


def init_or_reset_repo(repo_dir: str, origin_url: str, una_url: str, with_origin: bool = True) -> Repo:
    print(f"Initializing repo: {repo_dir}")
    if not os.path.exists(repo_dir):
        clone_url = origin_url if with_origin else una_url
        print(f"Cloning repo into {repo_dir} from {clone_url}...")
        repo = Repo.clone_from(clone_url, repo_dir, progress=TqdmProgress())
        if not with_origin:
            # If we cloned from una_url, it's currently named 'origin'. Rename it to 'una'.
            repo.remotes.origin.rename(remote_una_name)
    else:
        print(f"Repo exists at {repo_dir}; opening...")
        repo = Repo(repo_dir)

    print("Configuring SSH key...")
    repo.git.config("core.sshCommand", "ssh -i ~/.github.key -o IdentitiesOnly=yes")

    if with_origin:
        if "origin" not in [r.name for r in repo.remotes]:
            repo.create_remote("origin", origin_url)
        else:
            repo.remotes.origin.set_url(origin_url)
        
        print("Setting fetch refspec for origin...")
        repo.git.config("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")

        print("Fetching latest changes from origin...")
        repo.remotes.origin.fetch(progress=TqdmProgress())

    if remote_una_name not in [r.name for r in repo.remotes]:
        repo.create_remote(remote_una_name, una_url)
    else:
        repo.remotes[remote_una_name].set_url(una_url)

    print("Setting fetch refspec for una...")
    repo.git.config(f"remote.{remote_una_name}.fetch", f"+refs/heads/*:refs/remotes/{remote_una_name}/*")

    remote_una = repo.remotes[remote_una_name]
    print(f"Fetching latest changes from {remote_una_name} ({remote_una.url})...")
    try:
        remote_una.fetch(progress=TqdmProgress(), tags=True)
    except Exception as e:
        print(f"\nError: Failed to fetch from remote '{remote_una_name}' at {remote_una.url}")
        print(f"Please ensure the repository exists and you have access.")
        print(f"Git Error Details: {e}")
        sys.exit(1)

    # Check for local unpushed changes or dirty state before we reset
    unpushed = []
    try:
        if default_branch in repo.heads:
            local_branch = repo.heads[default_branch]
            remote_ref = repo.remotes[remote_una_name].refs[default_branch]
            unpushed = list(repo.iter_commits(f"{remote_ref.path}..{local_branch.path}"))
    except (IndexError, AttributeError):
        pass

    if unpushed or repo.is_dirty(untracked_files=True):
        print("\n" + "!" * 80)
        print(f"WARNING: Repository {repo_dir} has local changes that will be LOST!")
        if unpushed:
            print(f" - {len(unpushed)} unpushed commits on branch '{default_branch}'")
        if repo.is_dirty(untracked_files=True):
            print(" - Uncommitted or untracked changes in the working tree")
        print("!" * 80 + "\n")
        # In an interactive shell we might wait, but here we proceed as the script is automated.
        # However, specifically reporting it helps the user see WHY their files vanished.

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


def rebase_and_push(repo: Repo, branch_name: str, remote_name: str = remote_una_name):
    print(f"Rebasing current branch upon {branch_name}...")
    # These will raise exceptions on failure, which will stop the script
    repo.git.rebase(branch_name)

    print("Creating automatic rebase commit...")
    # allow-empty to ensure we always have the 'rebase' marker if requested
    repo.git.commit("--allow-empty", "-m", "rebase")

    print(f"Pushing rebased branch to {remote_name}...")
    refspec = f"refs/heads/{repo.active_branch.name}:refs/heads/{repo.active_branch.name}"
    repo.remotes[remote_name].push(refspec, force=True, progress=TqdmProgress())


def save_and_push(repo: Repo, branch_name: str, tag: str, remote_name: str = remote_una_name):
    print(f"Staging all changes in {repo.working_dir}...")
    repo.git.add(A=True)
    
    try:
        print(f"Committing with message: {tag}")
        repo.git.commit("-m", tag)
    except Exception as e:
        print(f"Nothing to commit or commit failed: {e}")

    # Rebase and push the branch first
    rebase_and_push(repo, branch_name, remote_name=remote_name)
    
    # Create and push tag on the final result
    print(f"Creating tag: {tag}")
    repo.create_tag(tag, force=True)
    
    print(f"Pushing tag '{tag}' to {remote_name}...")
    repo.remotes[remote_name].push(f"refs/tags/{tag}:refs/tags/{tag}", force=True)


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