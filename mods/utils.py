from git import Repo, RemoteProgress
import shutil
import os
import sys
from tqdm import tqdm

default_branch = "una"
remote_una_name = "una"


def get_remote_head(repo, remote_name):
    """
    Determines the default branch (HEAD) of a remote using 'git ls-remote --symref'.
    Also updates the local remote HEAD marker.
    """
    try:
        # First, try to update the local remote HEAD marker
        repo.git.remote("set-head", remote_name, "-a")
        
        # Then, try to get it from the symref
        out = repo.git.ls_remote("--symref", remote_name, "HEAD")
        for line in out.splitlines():
            if line.startswith("ref:"):
                # Example: "ref: refs/heads/main\tHEAD"
                ref_part = line.split()[1] # "refs/heads/main"
                return ref_part.rsplit("/", 1)[-1]
    except Exception as e:
        print(f"Warning: Could not determine HEAD for remote '{remote_name}': {e}")
    
    # Fallback to checking the local remote HEAD ref if set-head succeeded
    try:
        head_ref = repo.remotes[remote_name].refs.HEAD
        return head_ref.ref.name.rsplit("/", 1)[-1]
    except (IndexError, AttributeError, ValueError):
        pass

    return "master" # Final fallback



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


def init_or_reset_repo(repo_dir: str, origin_url: str, una_url: str, sparse_ignore_dirs: list, with_origin: bool = True, reset: bool = True, tag: str = None) -> Repo:
    """
    Initializes a repository or ensures an existing one has correct remotes and refspecs.
    If reset=True, it performs a hard reset to match the remote 'una' branch.
    If tag is provided, it checks out that specific tag.
    """
    print(f"Syncing repo: {repo_dir}")
    if not os.path.exists(repo_dir):
        clone_url = origin_url if with_origin else una_url
        print(f"Cloning repo into {repo_dir} from {clone_url}...")
        repo = Repo.clone_from(clone_url, repo_dir, progress=TqdmProgress())
        if not with_origin:
            # If we cloned from una_url, it's currently named 'origin'. Rename it to 'una'.
            repo.remotes.origin.rename(remote_una_name)
    else:
        # print(f"Repo exists at {repo_dir}; opening...")
        repo = Repo(repo_dir)

    # 1. Update Origin Remote
    if with_origin:
        if "origin" not in [r.name for r in repo.remotes]:
            repo.create_remote("origin", origin_url)
        else:
            if str(repo.remotes.origin.url) != origin_url:
                print(f"Updating origin URL for {repo_dir}")
                repo.remotes.origin.set_url(origin_url)
        
        # Ensure wildcard refspec so we see ALL branches (fixes the 'master' only issue)
        current_fetch = ""
        try: current_fetch = repo.git.config("--get", "remote.origin.fetch")
        except: pass
        
        if current_fetch != "+refs/heads/*:refs/remotes/origin/*":
            print(f"Updating origin fetch refspec for {repo_dir}...")
            repo.git.config("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")

        if reset:
            print("Fetching latest changes from origin...")
            repo.remotes.origin.fetch(progress=TqdmProgress(), prune=True)

    # 2. Update Una Remote
    if remote_una_name not in [r.name for r in repo.remotes]:
        repo.create_remote(remote_una_name, una_url)
    else:
        if str(repo.remotes[remote_una_name].url) != una_url:
             print(f"Updating una URL for {repo_dir}")
             repo.remotes[remote_una_name].set_url(una_url)

    # Ensure wildcard refspec for una
    repo.git.config(f"remote.{remote_una_name}.fetch", f"+refs/heads/*:refs/remotes/{remote_una_name}/*")

    if reset:
        print(f"Fetching latest changes from {remote_una_name}...")
        try:
            repo.remotes[remote_una_name].fetch(progress=TqdmProgress(), tags=True, prune=True)
        except Exception as e:
            print(f"\nError: Failed to fetch from remote '{remote_una_name}' at {una_url}")
            print(f"Git Error Details: {e}")
            sys.exit(1)

    # 3. Sparse Checkout Management
    if sparse_ignore_dirs:
        repo.config_writer().set_value("core", "sparseCheckout", "true").release()
        repo.config_writer().set_value("core", "sparseCheckoutCone", "false").release()
        repo.config_writer().set_value("index", "sparse", "true").release()
        try: repo.git.sparse_checkout("init")
        except: pass
        
        sparse_file = os.path.join(repo_dir, ".git", "info", "sparse-checkout")
        with open(sparse_file, "w") as f:
            f.write("/*\n")
            for ignore_dir in sparse_ignore_dirs:
                dir_pattern = ignore_dir.rstrip('/') + '/' if not ignore_dir.endswith('/') else ignore_dir
                f.write(f"!{dir_pattern}\n")
        
        if reset:
            repo.git.sparse_checkout("reapply")

    if not reset:
        return repo

    # 4. Mandatory Reset or Checkout
    if tag:
        try:
            remote_ref = repo.remotes.una.refs[default_branch]
            print(f"Found existing '{default_branch}' branch on remote. Checking it out to preserve patches...")
            if default_branch in repo.heads:
                repo.heads[default_branch].set_tracking_branch(remote_ref)
                repo.heads[default_branch].checkout()
            else:
                local_branch = repo.create_head(default_branch, remote_ref)
                local_branch.set_tracking_branch(remote_ref)
                local_branch.checkout()
            print(f"Note: You can now run '--rebase' to move your patches onto tag '{tag}'.")
        except (IndexError, AttributeError):
            print(f"No remote '{default_branch}' branch found. Initializing branch '{default_branch}' from tag '{tag}'...")
            # Start fresh from the tag since no project branch exists yet
            repo.git.checkout("-B", default_branch, tag)
        return repo

    # Reset to 'una' branch
    unpushed = []
    try:
        if default_branch in repo.heads:
            local_branch = repo.heads[default_branch]
            remote_ref = repo.remotes[remote_una_name].refs[default_branch]
            unpushed = list(repo.iter_commits(f"{remote_ref.path}..{local_branch.path}"))
    except: pass

    if unpushed or repo.is_dirty(untracked_files=True):
        print("\n" + "!" * 80)
        print(f"WARNING: Repository {repo_dir} has local changes that will be LOST during reset!")
        print("!" * 80 + "\n")

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

    repo.git.clean("-fdx")
    repo.head.reset(index=True, working_tree=True)

    return repo


def rebase_and_push(repo: Repo, branch_name: str, remote_name: str = remote_una_name, squash: bool = True, tag: str = None):
    print(f"Rebasing current branch upon {branch_name} (squash={squash}, tag={tag})...")
    
    if squash:
        # 1. Perform a real rebase first to ensure patches are correctly applied to the new code
        print(f"Applying patches via rebase onto {branch_name}...")
        repo.git.rebase(branch_name)
        
        # 2. Reset soft to the target branch to squash the results into one commit
        print("Squashing history into a single commit...")
        repo.git.reset("--soft", branch_name)
        
        # 3. Commit the squashed changes
        msg = f"una: squashed update from {branch_name}"
        if tag:
            msg = f"una: squashed update to tag {tag} (on {branch_name})"
            
        try:
            repo.git.commit("-m", msg)
        except:
            print("No changes to squash; already up to date.")
    else:
        # Standard rebase
        repo.git.rebase(branch_name)
        # Create an automatic rebase marker if not squashing
        repo.git.commit("--allow-empty", "-m", "rebase")

    print(f"Force-pushing rebased/squashed branch to {remote_name} (pruning history)...")
    try:
        current_branch_name = repo.active_branch.name
    except (TypeError, ValueError):
        current_branch_name = default_branch
        
    refspec = f"refs/heads/{current_branch_name}:refs/heads/{current_branch_name}"
    repo.remotes[remote_name].push(refspec, force=True, progress=TqdmProgress())


def save_and_push(repo: Repo, branch_name: str, tag: str, remote_name: str = remote_una_name):
    print(f"Staging all changes in {repo.working_dir}...")
    repo.git.add(A=True)
    
    try:
        print(f"Committing with message: {tag}")
        repo.git.commit("-m", tag)
    except Exception as e:
        print(f"Nothing to commit or commit failed: {e}")

    # Rebase latest and push the branch first (always squash for 'una' updates)
    rebase_and_push(repo, branch_name, remote_name=remote_name, squash=True)
    
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
