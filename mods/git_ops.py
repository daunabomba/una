"""
Git operations orchestration for una.
Wraps utils.py functions for repo-level workflows.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path
from git import Repo

from mods import colors
from mods.utils import (
    rebase_and_push as _rebase_and_push,
    save_and_push as _save_and_push,
    get_remote_head,
    TqdmProgress,
    init_or_reset_repo,
    is_repo_dirty,
    find_newer_tag,
)
from mods.trace import is_enabled, repo_created, repo_synced, repo_removed
from mods.snapshot import get_report_paths


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


def sync_repo(cfg: dict, una_base: str, base_dir: Path = None) -> bool:
    """
    Sync/initialize a single repository.

    Returns:
        True if repo was newly initialized, False otherwise
    """
    from pathlib import Path

    if base_dir is None:
        base_dir = Path(__file__).resolve().parent.parent

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

    repo = init_or_reset_repo(
        repo_dir=repo_dir,
        origin_url=cfg.get("origin_url"),
        una_url=una_url,
        sparse_ignore_dirs=cfg.get("sparse_ignore_dirs", []),
        with_origin=has_origin,
        reset=needs_reset,
        tag=cfg.get("tag"),
    )

    configured_tag = cfg.get("tag")
    if configured_tag and repo:
        try:
            repo_tags = [t.name for t in repo.tags]
            newer_tag = find_newer_tag(repo_tags, configured_tag)
            if newer_tag:
                cfg["newer_tag"] = newer_tag
                colors.warn(
                    f"Notice: A newer version tag '{newer_tag}' is available for '{cfg['name']}' (configured: '{configured_tag}')"
                )
                if auto_rebase_and_update_tag(
                    cfg["name"], configured_tag, newer_tag, repo_dir, base_dir
                ):
                    cfg["tag"] = newer_tag
                    cfg["rebased_new_tag"] = newer_tag
        except Exception as e:
            colors.warn(f"Warning: Failed to check/rebase tags for '{cfg['name']}': {e}")

    if is_enabled():
        if needs_reset:
            repo_created(cfg["name"], repo_dir)
        else:
            repo_synced(cfg["name"], repo_dir)

    return needs_reset


def auto_rebase_and_update_tag(
    comp_name: str,
    old_tag: str,
    new_tag: str,
    repo_dir: Path,
    base_dir: Path = None,
) -> bool:
    """
    Rebase a component's patches onto new_tag from old_tag, push with --force-with-lease,
    update the .repo file on disk, and commit/push the top-level una repository update.
    """
    import re as _re
    import subprocess

    if base_dir is None:
        base_dir = Path(__file__).resolve().parent.parent

    repo = Repo(repo_dir)

    print(f"\n--- Rebasing {comp_name}: {old_tag} -> {new_tag} ---")
    print(f">>> git rebase --onto={new_tag} {old_tag}  (in {repo_dir})")
    result = subprocess.run(
        ["git", "rebase", f"--onto={new_tag}", old_tag],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        colors.warn(f"\nRebase failed for {comp_name} ({old_tag} -> {new_tag}). Attempting to abort...")
        print(">>> git rebase --abort")
        subprocess.run(["git", "rebase", "--abort"], cwd=repo_dir)
        colors.warn(
            f"Rebase aborted for {comp_name}. Resolve conflicts manually if desired."
        )
        return False

    colors.info(f"Rebase succeeded for {comp_name}.")

    # Push with force-with-lease in sub-repository
    remote_names = [r.name for r in repo.remotes]
    remote_name = "una" if "una" in remote_names else ("origin" if "origin" in remote_names else None)
    if remote_name:
        try:
            active_branch = repo.active_branch.name
        except Exception:
            active_branch = "una"

        refspec = f"refs/heads/{active_branch}:refs/heads/{active_branch}"
        print(f">>> git push --force-with-lease {remote_name} {refspec}  (in {repo_dir})")
        try:
            subprocess.run(
                ["git", "push", "--force-with-lease", remote_name, refspec],
                cwd=repo_dir,
                check=True,
                capture_output=True,
                text=True,
            )
            colors.info(f"Pushed {comp_name} to {remote_name}/{active_branch} with --force-with-lease.")
        except Exception as e:
            colors.warn(f"Warning: git push --force-with-lease failed for {comp_name}: {e}")

    # Update .repo file
    repo_file = _find_repo_file_with_tag(comp_name, base_dir)
    if repo_file and repo_file.exists():
        print(f"\nUpdating tag in {repo_file.relative_to(base_dir)}: {old_tag} -> {new_tag}")
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

            # Commit and push in top-level una repo
            try:
                top_repo = Repo(base_dir)
                rel_path = str(repo_file.relative_to(base_dir))
                print(f">>> git add {rel_path}  (in {base_dir})")
                top_repo.git.add(rel_path)

                commit_msg = f"updating tag on {comp_name} from {old_tag} to {new_tag}"
                print(f">>> git commit -m '{commit_msg}'  (in {base_dir})")
                top_repo.git.commit("-m", commit_msg)

                top_remote_names = [r.name for r in top_repo.remotes]
                top_remote = "una" if "una" in top_remote_names else ("origin" if "origin" in top_remote_names else None)
                if top_remote:
                    try:
                        top_branch = top_repo.active_branch.name
                    except Exception:
                        top_branch = "una"
                    top_refspec = f"refs/heads/{top_branch}:refs/heads/{top_branch}"
                    print(f">>> git push {top_remote} {top_refspec}  (in {base_dir})")
                    try:
                        subprocess.run(
                            ["git", "push", top_remote, top_refspec],
                            cwd=base_dir,
                            check=True,
                            capture_output=True,
                            text=True,
                        )
                        colors.info(f"Pushed top-level repository commit to {top_remote}/{top_branch}.")
                    except Exception as pe:
                        colors.warn(f"Warning: Failed to push top-level repo changes: {pe}")
            except Exception as ce:
                colors.warn(f"Warning: Failed to commit top-level repo changes: {ce}")
    else:
        colors.warn(
            f"Warning: could not locate a .repo file with 'tag = ...' for '{comp_name}'. Tag not updated on disk."
        )

    # Show diff stat
    print(f"\n>>> git diff {old_tag} --stat  (in {repo_dir})")
    subprocess.run(["git", "diff", old_tag, "--stat"], cwd=repo_dir)

    return True


def rebase_to_tag(comp_name: str, new_tag: str, repos_config: list, base_dir: Path):
    """
    Rebase a component's local patches onto a new upstream tag.
    """
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

    repo = Repo(repo_dir)

    # Fetch tags from remote
    remote_names = [r.name for r in repo.remotes]
    if "una" in remote_names:
        print(f">>> git fetch --tags una")
        repo.remotes.una.fetch(tags=True, progress=TqdmProgress())
    elif "origin" in remote_names:
        print(f">>> git fetch --tags origin")
        repo.remotes.origin.fetch(tags=True, progress=TqdmProgress())

    auto_rebase_and_update_tag(comp_name, old_tag, new_tag, repo_dir, base_dir)


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


def sync_kernel_config(src: Path, dest: Path):
    """
    Syncs back the updated kernel config, stripping leading comments
    and forcing specific values like CONFIG_CC_VERSION_TEXT.
    """
    if not src.exists():
        return
    content = src.read_text()
    lines = content.splitlines()
    out_lines = []

    # Skip leading comments and empty lines
    header_done = False
    for line in lines:
        if not header_done:
            if line.strip().startswith("#") or not line.strip():
                continue
            else:
                header_done = True

        # Process entries
        if line.startswith("CONFIG_CC_VERSION_TEXT="):
            out_lines.append('CONFIG_CC_VERSION_TEXT="clang"')
        else:
            out_lines.append(line)

    dest.write_text("\n".join(out_lines) + "\n")


def get_git_remote_base(remote_name="una"):
    """
    Attempts to determine the base URL of the current git repository's remote.
    Specifically looks for the remote specified by remote_name.
    """
    try:
        script_dir = Path(__file__).resolve().parent.parent
        repo = Repo(script_dir, search_parent_directories=True)

        for r in repo.remotes:
            if r.name == remote_name:
                url = str(r.url)
                if "/" in url:
                    return url.rsplit("/", 1)[0]
    except Exception:
        pass
    return None


def remove_repo(name, repos, arches, bld_base):
    """Removes a repository from the list, cleans build outputs and deletes repo dir."""
    target = next((r for r in repos if r["name"] == name), None)
    if not target:
        colors.error(f"Error: Repository '{name}' not found.")
        return False

    colors.info(f"Removing repository '{name}'...")

    # 1. Clean build outputs for each architecture
    for arch in arches:
        report_file = bld_base / arch / "report" / f"{name}.txt"
        if report_file.exists():
            colors.info(f"[{arch}] Cleaning build outputs for {name}...")
            paths = get_report_paths(report_file)
            staging_dir = bld_base / "staging"
            target_dir = bld_base / "target"

            for p in paths:
                try:
                    if p.startswith("staging/"):
                        (staging_dir / p[8:]).unlink(missing_ok=True)
                    elif p.startswith("target/"):
                        (target_dir / p[7:]).unlink(missing_ok=True)
                except Exception as e:
                    colors.warn(f"[{arch}] Warning: Failed to remove {p}: {e}")
            report_file.unlink()

    # 2. Delete the repository directory
    repo_dir = Path(target["repo_dir"])
    if repo_dir.exists():
        print(f"Deleting repository directory: {repo_dir}")
        if is_enabled():
            repo_removed(name, repo_dir)
        shutil.rmtree(repo_dir)

    # 3. Remove from repos list to prevent sync attempts
    repos[:] = [r for r in repos if r["name"] != name]

    colors.info(f"Repository '{name}' removed successfully.")
    return True

