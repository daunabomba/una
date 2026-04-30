"""
Git operations orchestration for una.
Wraps utils.py functions for repo-level workflows.
"""

from pathlib import Path
from git import Repo

from mods import colors
from mods.utils import (
    rebase_and_push as _rebase_and_push,
    save_and_push as _save_and_push,
    get_remote_head,
    TqdmProgress,
    init_or_reset_repo,
)
from mods.trace import is_enabled, repo_created, repo_synced


def handle_top_level_repo(
    base_dir: Path, action: str, tag: str = None, squash: bool = True
):
    """
    Handle git operations for the top-level una repository.

    Args:
        base_dir: Path to top-level repo
        action: One of 'rebase', 'save', 'checkout'
        tag: For save/checkout operations
        squash: Whether to squash commits on rebase
    """
    top_repo = Repo(base_dir)

    print(f"\n--- Top-level Repository (una) ---")

    if action == "rebase":
        print("Fetching from 'una'...")
        top_repo.remotes.una.fetch(progress=TqdmProgress())
        _rebase_and_push(top_repo, "una/una", remote_name="una", squash=squash)

    elif action == "save":
        print("Fetching from 'una'...")
        top_repo.remotes.una.fetch(progress=TqdmProgress())
        _save_and_push(top_repo, "una/una", tag, remote_name="una")

    elif action == "checkout":
        print("Fetching tags for top-level repo...")
        top_repo.remotes.una.fetch(tags=True)
        if tag:
            print(f"Checking out tag '{tag}'...")
            try:
                top_repo.git.checkout(tag)
            except Exception as e:
                colors.error(f"Error checking out tag '{tag}' in top-level repo: {e}")


def handle_repos(repos: list, action: str, tag: str = None, include_all: bool = True):
    """
    Handle git operations for sub-repositories.

    Args:
        repos: List of repo configs
        action: One of 'rebase', 'save', 'checkout', 'status'
        tag: For save/checkout operations
        include_all: If False, only process repos matching action param
    """
    from mods.utils import is_repo_dirty

    processed_dirs = set()

    for cfg in repos:
        r_path = Path(cfg["repo_dir"]).absolute()
        if r_path in processed_dirs:
            continue

        if action == "status":
            _handle_status(cfg, r_path)
        elif include_all or action == cfg["name"]:
            _handle_repo_operation(cfg, r_path, action, tag)

        processed_dirs.add(r_path)


def _handle_status(cfg: dict, r_path: Path):
    """Handle git status for a single repo."""
    import subprocess

    if r_path.exists() and (r_path / ".git").exists():
        print(f"\n=== Repository: {cfg['name']} ({r_path}) ===")
        subprocess.run(["git", "status", "-sb"], cwd=r_path)
    elif r_path.exists():
        print(f"\n=== Repository: {cfg['name']} ({r_path}) [Not a Git Repo] ===")
    else:
        print(f"\n=== Repository: {cfg['name']} ({r_path}) [MISSING] ===")


def _handle_repo_operation(cfg: dict, r_path: Path, action: str, tag: str = None):
    """Handle rebase/save/checkout for a single repo."""
    if not r_path.exists() or not (r_path / ".git").exists():
        return

    print(f"\n--- Repository: {cfg['name']} ({r_path}) ---")
    repo = Repo(r_path)

    remote_prefix = "origin" if "origin_url" in cfg else "una"

    print(f"Fetching from {remote_prefix}...")
    repo.remotes[remote_prefix].fetch(progress=TqdmProgress())
    if remote_prefix == "origin" and "una" in repo.remotes:
        print("Also fetching from una...")
        repo.remotes.una.fetch(progress=TqdmProgress())

    if remote_prefix == "una":
        target_branch = "una/una"
    else:
        branch = cfg.get("branch")
        tag_name = cfg.get("tag")

        if branch:
            target_branch = f"{remote_prefix}/{branch}"
        elif tag_name:
            target_branch = tag_name
        else:
            target_branch = f"{remote_prefix}/{get_remote_head(repo, remote_prefix)}"

    if action == "save" and tag:
        _save_and_push(repo, target_branch, tag)
    elif action == "rebase":
        _rebase_and_push(repo, target_branch, squash=True, tag=cfg.get("tag"))
    elif action == "checkout" and tag:
        print(f"Checking out tag '{tag}'...")
        try:
            repo.git.checkout(tag)
        except Exception as e:
            colors.error(f"Error checking out tag '{tag}' in {cfg['name']}: {e}")


def print_top_level_status(base_dir: Path):
    """Print git status for top-level repository."""
    import subprocess

    print("=== Top-level Repository (una) ===")
    subprocess.run(["git", "status", "-sb"], cwd=base_dir)


def sync_repo(cfg: dict, una_base: str) -> bool:
    """
    Sync/initialize a single repository.

    Returns:
        True if repo was newly initialized, False otherwise
    """
    from pathlib import Path

    repo_dir = Path(cfg["repo_dir"])
    needs_reset = not repo_dir.exists()

    if needs_reset:
        if not una_base:
            colors.warn(
                f"Warning: New repository '{cfg['name']}' found in config but 'una' base URL is unknown. "
                "Please ensure the top-level repository has a remote named 'una'. Skipping initialization."
            )
            return False
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
        sparse_ignore_dirs=cfg.get("sparse_ignore_dirs", []),
        with_origin=has_origin,
        reset=needs_reset,
        tag=cfg.get("tag"),
    )

    if is_enabled():
        if needs_reset:
            repo_created(cfg["name"], repo_dir)
        else:
            repo_synced(cfg["name"], repo_dir)

    return needs_reset
