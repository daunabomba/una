from git import Repo, RemoteProgress
import shutil
import os
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
    remote_ref = repo.remotes.una.refs[default_branch]
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