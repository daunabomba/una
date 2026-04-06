from git import Repo, RemoteProgress
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
            ncols=-1,
        )

    def update(self, op_code, cur_count, max_count=None, message=""):
        if max_count is None:
            max_count = 1

        self.pbar.n = cur_count
        self.pbar.total = max_count
        self.pbar.set_description(message or self.pbar.desc)
        self.pbar.refresh()

        if cur_count == max_count:
            self.pbar.close()

    def __del__(self):
        if not hasattr(self, "pbar"):
            return
        try:
            if not self.pbar.disable:
                self.pbar.close()
        except (AttributeError, RuntimeError):
            pass


def init_or_reset_repo(repo_dir: str, origin_url: str, una_url: str) -> Repo:
    if not os.path.exists(repo_dir):
        print(f"Cloning repo into {repo_dir}...")
        repo = Repo.clone_from(origin_url, repo_dir, progress=TqdmProgress())
    else:
        print(f"Repo exists at {repo_dir}; opening...")
        repo = Repo(repo_dir)

    if "origin" not in [r.name for r in repo.remotes]:
        repo.create_remote("origin", origin_url)
    else:
        repo.remotes.origin.set_url(origin_url)

    if remote_una_name not in [r.name for r in repo.remotes]:
        repo.create_remote(remote_una_name, una_url)
    else:
        repo.remotes[remote_una_name].set_url(una_url)

    print("Fetching latest changes from origin...")
    repo.remotes.origin.fetch(progress=TqdmProgress())

    print("Fetching latest changes from una...")
    repo.remotes.una.fetch(progress=TqdmProgress())

    print("Checking out branch una from remote-tracking branch una/una...")
    if default_branch in repo.heads:
        repo.heads[default_branch].checkout()
    else:
        repo.git.checkout("-b", default_branch, f"{remote_una_name}/{default_branch}")

    print("Running git clean -fdx...")
    repo.git.clean("-fdx")

    print("Running git reset --hard...")
    repo.head.reset(index=True, working_tree=True)

    return repo
