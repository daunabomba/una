from git import Repo, RemoteProgress
import shutil
import os
import sys
from pathlib import Path
from tqdm import tqdm
from mods import colors
from mods.trace import is_enabled, repo_synced, repo_created, repo_removed

default_branch = "una"
remote_una_name = "una"


def get_remote_head(repo, remote_name):
    """
    Determines the default branch (HEAD) of a remote.
    Prioritizes live 'ls-remote' discovery to ensure we follow the upstream move (e.g. master -> main).
    """
    # 1. Try network discovery (Source of Truth)
    try:
        out = repo.git.ls_remote("--symref", remote_name, "HEAD")
        for line in out.splitlines():
            if line.startswith("ref:"):
                # Example: "ref: refs/heads/main\tHEAD"
                ref_part = line.split()[1]  # "refs/heads/main"
                branch = ref_part.rsplit("/", 1)[-1]
                # Update local remote HEAD marker for future fast-lookups
                try:
                    repo.git.remote("set-head", remote_name, branch)
                except:
                    pass
                return branch
    except Exception as e:
        colors.warn(
            f"Warning: ls-remote discovery of HEAD for remote '{remote_name}' failed: {e}"
        )

    # 2. Fallback to local remote HEAD ref
    try:
        head_ref = repo.remotes[remote_name].refs.HEAD
        return head_ref.ref.name.rsplit("/", 1)[-1]
    except (IndexError, AttributeError, ValueError, Exception):
        pass

    # 3. Last resort fallbacks based on existing local remote refs
    for b in ["master", "main"]:
        try:
            if f"refs/remotes/{remote_name}/{b}" in repo.refs:
                return b
        except:
            pass

    return "master"  # Final fallback


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


def init_or_reset_repo(
    repo_dir: str,
    origin_url: str,
    una_url: str,
    sparse_ignore_dirs: list,
    with_origin: bool = True,
    reset: bool = True,
    tag: str = None,
) -> Repo:
    """
    Initializes a repository or ensures an existing one has correct remotes and refspecs.
    If reset=True, it performs a hard reset to match the remote 'una' branch.
    If tag is provided, it checks out that specific tag.
    """
    colors.info(f"Syncing repo: {repo_dir}")
    if not os.path.exists(repo_dir):
        clone_url = origin_url if with_origin else una_url
        colors.info(f"Cloning repo into {repo_dir} from {clone_url}...")
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
                colors.info(f"Updating origin URL for {repo_dir}")
                repo.remotes.origin.set_url(origin_url)

        # Ensure wildcard refspec so we see ALL branches (fixes the 'master' only issue)
        current_fetch = ""
        try:
            current_fetch = repo.git.config("--get", "remote.origin.fetch")
        except:
            pass

        if current_fetch != "+refs/heads/*:refs/remotes/origin/*":
            colors.info(f"Updating origin fetch refspec for {repo_dir}...")
            repo.git.config(
                "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"
            )

        if reset:
            colors.info("Fetching latest changes from origin...")
            repo.remotes.origin.fetch(progress=TqdmProgress(), prune=True)

    # 2. Update Una Remote
    if remote_una_name not in [r.name for r in repo.remotes]:
        repo.create_remote(remote_una_name, una_url)
    else:
        if str(repo.remotes[remote_una_name].url) != una_url:
            colors.info(f"Updating una URL for {repo_dir}")
            repo.remotes[remote_una_name].set_url(una_url)

    # Ensure wildcard refspec for una
    repo.git.config(
        f"remote.{remote_una_name}.fetch",
        f"+refs/heads/*:refs/remotes/{remote_una_name}/*",
    )

    if reset:
        colors.info(f"Fetching latest changes from {remote_una_name}...")
        try:
            if is_enabled():
                from mods.trace import repo_synced
                repo_synced(repo_dir, Path(repo_dir))
            repo.remotes[remote_una_name].fetch(
                progress=TqdmProgress(), tags=True, prune=True
            )
        except Exception as e:
            colors.error(
                f"\nError: Failed to fetch from remote '{remote_una_name}' at {una_url}"
            )
            colors.error(f"Git Error Details: {e}")
            sys.exit(1)

    # 3. Sparse Checkout Management
    if sparse_ignore_dirs:
        repo.config_writer().set_value("core", "sparseCheckout", "true").release()
        repo.config_writer().set_value("core", "sparseCheckoutCone", "false").release()
        repo.config_writer().set_value("index", "sparse", "true").release()
        try:
            repo.git.sparse_checkout("init")
        except:
            pass

        sparse_file = os.path.join(repo_dir, ".git", "info", "sparse-checkout")
        with open(sparse_file, "w") as f:
            f.write("/*\n")
            for ignore_dir in sparse_ignore_dirs:
                dir_pattern = (
                    ignore_dir.rstrip("/") + "/"
                    if not ignore_dir.endswith("/")
                    else ignore_dir
                )
                f.write(f"!{dir_pattern}\n")

        if reset:
            repo.git.sparse_checkout("reapply")

    if not reset:
        return repo

    # 4. Mandatory Reset or Checkout
    if tag:
        try:
            remote_ref = repo.remotes.una.refs[default_branch]
            colors.info(
                f"Found existing '{default_branch}' branch on remote. Checking it out to preserve patches..."
            )
            if default_branch in repo.heads:
                repo.heads[default_branch].set_tracking_branch(remote_ref)
                repo.heads[default_branch].checkout()
            else:
                local_branch = repo.create_head(default_branch, remote_ref)
                local_branch.set_tracking_branch(remote_ref)
                local_branch.checkout()
            colors.info(
                f"Note: You can now run '--rebase' to move your patches onto tag '{tag}'."
            )
        except (IndexError, AttributeError):
            colors.info(
                f"No remote '{default_branch}' branch found. Initializing branch '{default_branch}' from tag '{tag}'..."
            )
            # Start fresh from the tag since no project branch exists yet
            repo.git.checkout("-B", default_branch, tag)
        return repo

    # Reset to 'una' branch
    unpushed = []
    try:
        if default_branch in repo.heads:
            local_branch = repo.heads[default_branch]
            remote_ref = repo.remotes[remote_una_name].refs[default_branch]
            unpushed = list(
                repo.iter_commits(f"{remote_ref.path}..{local_branch.path}")
            )
    except:
        pass

    if unpushed or repo.is_dirty(untracked_files=True):
        colors.warn("\n" + "!" * 80)
        colors.warn(
            f"WARNING: Repository {repo_dir} has local changes that will be LOST during reset!"
        )
        colors.warn("!" * 80 + "\n")

    try:
        remote_ref = repo.remotes.una.refs[default_branch]
    except (IndexError, AttributeError):
        colors.error(
            f"Error: Branch '{default_branch}' not found on remote '{remote_una_name}'."
        )
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


def rebase_and_push(
    repo: Repo,
    branch_name: str,
    remote_name: str = remote_una_name,
    squash: bool = True,
    tag: str = None,
):
    colors.info(
        f"Rebasing current branch upon {branch_name} (squash={squash}, tag={tag})..."
    )

    try:
        if squash:
            # 1. Perform a real rebase first to ensure patches are correctly applied to the new code
            colors.info(f"Applying patches via rebase onto {branch_name}...")
            if is_enabled():
                from mods.trace import build_step_start, build_step_end
                build_step_start('git', repo_dir, 'rebase')
            repo.git.rebase(branch_name)
            if is_enabled():
                build_step_end('git', repo_dir, 'rebase')

            # Check if we are actually different from the base.
            # If our tree is identical to the base, and we have no commits to squash, we should skip.
            if repo.head.commit.tree == repo.commit(branch_name).tree:
                colors.info(
                    f"Already up to date with {branch_name}; skipping squash commit."
                )
                # We still want to push if we just moved our branch to match upstream
            else:
                # 2. Reset soft to the target branch to squash the results into one commit
                colors.info("Squashing history into a single commit...")
                repo.git.reset("--soft", branch_name)

                # 3. Commit the squashed changes
                msg = f"una: squashed update from {branch_name}"
                if tag:
                    msg = f"una: squashed update to tag {tag} (on {branch_name})"

                try:
                    repo.git.commit("-m", msg)
                except:
                    colors.info("No changes to squash; already up to date.")
        else:
            # Standard rebase
            if is_enabled():
                from mods.trace import build_step_start, build_step_end
                build_step_start('git', repo_dir, 'rebase')
            repo.git.rebase(branch_name)
            if is_enabled():
                build_step_end('git', repo_dir, 'rebase')
            # Create an automatic rebase marker if not squashing
            repo.git.commit("--allow-empty", "-m", "rebase")
    except Exception as e:
        colors.error(f"\nERROR: Rebase failed for {repo.working_dir}")
        colors.error(f"Target: {branch_name}")
        colors.error(f"Details: {e}")
        try:
            print("Attempting to abort rebase...")
            repo.git.rebase("--abort")
        except:
            pass
        raise

    colors.info(
        f"Force-pushing rebased/squashed branch to {remote_name} (pruning history)..."
    )

    print(
        f"Force-pushing rebased/squashed branch to {remote_name} (pruning history)..."
    )
    try:
        current_branch_name = repo.active_branch.name
    except (TypeError, ValueError):
        current_branch_name = default_branch

    refspec = f"refs/heads/{current_branch_name}:refs/heads/{current_branch_name}"
    repo.remotes[remote_name].push(refspec, force=True, progress=TqdmProgress())


def save_and_push(
    repo: Repo, branch_name: str, tag: str, remote_name: str = remote_una_name
):
    colors.info(f"Staging all changes in {repo.working_dir}...")
    repo.git.add(A=True)

    try:
        colors.info(f"Committing with message: {tag}")
        repo.git.commit("-m", tag)
    except Exception as e:
        colors.warn(f"Nothing to commit or commit failed: {e}")

    # Rebase latest and push the branch first (always squash for 'una' updates)
    rebase_and_push(repo, branch_name, remote_name=remote_name, squash=True)

    # Create and push tag on the final result
    colors.info(f"Creating tag: {tag}")
    repo.create_tag(tag, force=True)

    colors.info(f"Pushing tag '{tag}' to {remote_name}...")
    repo.remotes[remote_name].push(f"refs/tags/{tag}:refs/tags/{tag}", force=True)


def get_all_arches() -> list:
    """Return list of all supported architectures."""
    return ["x32", "x86_64", "aarch64", "riscv64"]


def get_target_triple(arch: str) -> str:
    if arch == "x32":
        return "x32-linux-muslx32"
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
    return {"x32": "x86", "x86_64": "x86", "aarch64": "arm64", "riscv64": "riscv"}.get(
        arch, "x86"
    )


def is_repo_dirty(repo_path: Path):
    """
    Check if a git repository has any modified or untracked files.
    """
    import subprocess

    if not (repo_path / ".git").exists():
        return False
    result = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo_path, capture_output=True, text=True
    )
    return len(result.stdout.strip()) > 0


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
