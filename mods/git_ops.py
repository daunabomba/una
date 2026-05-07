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
        print(f">>> git fetch una")
        top_repo.remotes.una.fetch(progress=TqdmProgress())
        _rebase_and_push(top_repo, "una/una", remote_name="una", squash=squash)

    elif action == "save":
        print(f">>> git fetch una")
        top_repo.remotes.una.fetch(progress=TqdmProgress())
        _save_and_push(top_repo, "una/una", tag, remote_name="una")

    elif action == "checkout":
        print(f">>> git fetch --tags una")
        top_repo.remotes.una.fetch(tags=True, progress=TqdmProgress())
        if tag:
            print(f">>> git checkout {tag}")
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
        print(f">>> git status -sb")
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

    print(f">>> git fetch {remote_prefix}")
    repo.remotes[remote_prefix].fetch(progress=TqdmProgress())
    if remote_prefix == "origin" and "una" in repo.remotes:
        print(f">>> git fetch una")
        repo.remotes.una.fetch(progress=TqdmProgress())

    if remote_prefix == "una":
        target_branch = "una/una"
    else:
        branch = cfg.get("branch")
        tag_name = cfg.get("tag")

        if branch:
            target_branch = f"{remote_prefix}/{branch}"
        elif tag_name and action in ["checkout", "save"]:
            # Only use tag for checkout and save, not for rebase
            target_branch = tag_name
        else:
            target_branch = f"{remote_prefix}/{get_remote_head(repo, remote_prefix)}"

    if action == "save" and tag:
        _save_and_push(repo, target_branch, tag)
    elif action == "rebase":
        # For rebase, ignore tag - always rebase onto the appropriate branch
        _rebase_and_push(repo, target_branch, squash=True, tag=cfg.get("tag"))
    elif action == "checkout" and tag:
        print(f">>> git checkout {tag}")
        try:
            repo.git.checkout(tag)
        except Exception as e:
            colors.error(f"Error checking out tag '{tag}' in {cfg['name']}: {e}")


def print_top_level_status(base_dir: Path):
    """Print git status for top-level repository."""
    import subprocess

    print("=== Top-level Repository (una) ===")
    print(f">>> git status -sb")
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


def rebase_to_tag(comp_name: str, new_tag: str, repos_config: list, base_dir: Path):
    """
    Rebase a component's local patches onto a new upstream tag.

    Steps:
      1. Locate the component in repos_config — it must have a 'tag' field.
      2. Fetch tags from the remote so new_tag is available locally.
      3. Dereference old_tag to its commit hash (annotated tags are tag objects,
         not commits — git merge-base refuses them directly).
      4. Find the fork-point: git merge-base --fork-point <old_tag_commit>
         This uses the reflog to find where our branch diverged from the tag.
      5. Rebase: git rebase --onto=<new_tag> <fork_point>
      6. On success, update tag= in the .repo file that actually owns it
         (following ref= chains, e.g. llvm-runtime → llvm-tools).
      7. Show: git diff <old_tag> --stat
    """
    import subprocess
    import re as _re

    # ── 1. Find component config ──────────────────────────────────────────────
    cfg = next((r for r in repos_config if r["name"] == comp_name), None)
    if cfg is None:
        colors.error(f"Error: component '{comp_name}' not found in configuration.")
        return

    old_tag = cfg.get("tag")
    if not old_tag:
        colors.error(
            f"Error: component '{comp_name}' has no 'tag' field in its config. "
            "Only tag-pinned repos can be rebased with this command."
        )
        return

    repo_dir = Path(cfg["repo_dir"])
    if not repo_dir.exists() or not (repo_dir / ".git").exists():
        colors.error(
            f"Error: repository directory '{repo_dir}' does not exist or is not a git repo."
        )
        return

    print(f"\n--- Rebasing {comp_name}: {old_tag} → {new_tag} ---")
    repo = Repo(repo_dir)

    # ── 2. Fetch tags from remote ─────────────────────────────────────────────
    remote_names = [r.name for r in repo.remotes]
    if "una" in remote_names:
        print(f">>> git fetch --tags una")
        repo.remotes.una.fetch(tags=True, progress=TqdmProgress())
    elif "origin" in remote_names:
        print(f">>> git fetch --tags origin")
        repo.remotes.origin.fetch(tags=True, progress=TqdmProgress())
    else:
        colors.error("Error: no 'una' or 'origin' remote found.")
        return

    # ── 3. Dereference old_tag to its commit ──────────────────────────────────
    # Annotated tags are tag objects, not commits. git merge-base --fork-point
    # requires a commit, so we peel the tag with ^{commit}.
    print(f">>> git rev-parse {old_tag}^{{commit}}")
    result = subprocess.run(
        ["git", "rev-parse", f"{old_tag}^{{commit}}"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        colors.error(f"Error: could not resolve '{old_tag}' to a commit.")
        colors.error(result.stderr.strip())
        return
    old_tag_commit = result.stdout.strip()
    print(f"    {old_tag} → {old_tag_commit}")

    # ── 4. Find fork-point via reflog ─────────────────────────────────────────
    # git merge-base --fork-point <commit> uses the reflog of the current branch
    # to find the most recent point where it diverged from <commit>.
    print(f">>> git merge-base --fork-point {old_tag_commit}")
    result = subprocess.run(
        ["git", "merge-base", "--fork-point", old_tag_commit],
        cwd=repo_dir, capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        # Fork-point not found in reflog — fall back to the tag commit itself,
        # which rebases the full patch set onto new_tag.
        colors.warn(
            f"Warning: no fork-point found in reflog for {old_tag_commit}; "
            "using the tag commit directly as upstream."
        )
        fork_point = old_tag_commit
    else:
        fork_point = result.stdout.strip()
    print(f"    fork-point: {fork_point}")

    # ── 5. Rebase: git rebase --onto=<new_tag> <fork_point> ──────────────────
    print(f">>> git rebase --onto={new_tag} {fork_point}")
    result = subprocess.run(
        ["git", "rebase", f"--onto={new_tag}", fork_point],
        cwd=repo_dir, capture_output=False,  # stream output live
    )
    if result.returncode != 0:
        colors.error("\nRebase failed. Attempting to abort...")
        print(">>> git rebase --abort")
        subprocess.run(["git", "rebase", "--abort"], cwd=repo_dir)
        colors.error(
            "Rebase aborted. Resolve conflicts manually, then re-run --rebase."
        )
        return

    colors.info("Rebase succeeded.")

    # ── 6. Update tag= in the .repo file that owns it ────────────────────────
    # Components may use ref= to inherit fields (e.g. llvm-runtime → llvm-tools).
    # We follow the ref chain to find the .repo file that actually has tag=.
    repo_file = _find_repo_file_with_tag(comp_name, base_dir)
    if repo_file:
        print(f"\nUpdating tag in {repo_file.relative_to(base_dir)}: {old_tag} → {new_tag}")
        text = repo_file.read_text()
        updated = _re.sub(
            r"(?m)^(\s*tag\s*=\s*)\S+",
            lambda m: m.group(1) + new_tag,
            text,
        )
        if updated == text:
            colors.warn(
                f"Warning: 'tag = {old_tag}' not found in {repo_file.name}; file not updated."
            )
        else:
            repo_file.write_text(updated)
            colors.info(f"Updated {repo_file.name}: tag = {new_tag}")
    else:
        colors.warn(
            f"Warning: could not locate a .repo file with 'tag = ...' for "
            f"'{comp_name}' under {base_dir}/confs/repos/. Tag not updated on disk."
        )

    # ── 7. Show diff stat ─────────────────────────────────────────────────────
    print(f"\n>>> git diff {old_tag} --stat")
    subprocess.run(["git", "diff", old_tag, "--stat"], cwd=repo_dir)


def _find_repo_file_with_tag(comp_name: str, base_dir: Path) -> Path:
    """
    Search confs/repos/ for the .repo file that owns the 'tag =' field for
    comp_name. Follows ref= chains (e.g. llvm-runtime ref= llvm-tools).
    Returns the Path of the .repo file containing the tag= field, or None.
    """
    import configparser

    repos_dir = base_dir / "confs" / "repos"
    if not repos_dir.exists():
        return None

    # Build a map: section_name -> (file_path, configparser_section)
    section_map = {}
    for repo_file in repos_dir.glob("*.repo"):
        cp = configparser.ConfigParser()
        cp.read(repo_file)
        for section in cp.sections():
            section_map[section] = (repo_file, dict(cp[section]))

    # Follow the ref= chain from comp_name
    visited = set()
    current = comp_name
    while current and current not in visited:
        visited.add(current)
        if current not in section_map:
            break
        file_path, fields = section_map[current]
        if "tag" in fields:
            return file_path
        # Follow ref= if present
        current = fields.get("ref")

    return None
