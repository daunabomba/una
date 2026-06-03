"""
Configuration loading and validation for una.
"""

import configparser
import json
import sys
from pathlib import Path
from typing import Optional

from mods import colors

BASE_DIR = None

REQUIRED_FIELDS = {
    "target": ["repo_dir", "type"],
    "tools": ["repo_dir", "type"],
    "virtual": ["type"],
    "default": ["repo_dir", "type"],
}


def set_base_dir(base_dir: Path):
    """Set the base directory for config operations."""
    global BASE_DIR
    BASE_DIR = base_dir


class ConfigError(Exception):
    """Raised for configuration errors (circular refs, missing refs, etc.)."""

    pass


def validate_repo_config(cfg: dict, all_repo_names: set, skip_fields: set) -> list:
    """
    Validate a single repo configuration.
    Returns list of error messages (empty if valid).
    """
    errors = []
    name = cfg.get("name", "<unnamed>")
    repo_type = cfg.get("type", "")

    fields_to_check = REQUIRED_FIELDS.get(repo_type, REQUIRED_FIELDS["default"])
    for field in fields_to_check:
        if field not in cfg:
            errors.append(f"Missing required field '{field}' in repo '{name}'")

    if "depends" in cfg:
        deps = cfg["depends"] if isinstance(cfg["depends"], list) else []
        for dep in deps:
            if dep not in all_repo_names:
                errors.append(f"Repo '{name}' depends on non-existent repo '{dep}'")

    return errors


def detect_circular_refs(raw_configs: dict) -> Optional[str]:
    """
    Detect circular references in config 'ref' chain.
    Returns cycle path string if found, None otherwise.
    """
    for name in raw_configs:
        visited = []
        current = name
        while True:
            if current in visited:
                cycle_start = visited.index(current)
                cycle = visited[cycle_start:] + [current]
                return " -> ".join(cycle)
            if current not in raw_configs:
                break
            visited.append(current)
            current = raw_configs[current].get("ref")
            if not current:
                break
    return None


def resolve_ref(cfg: dict, raw_configs: dict) -> dict:
    """
    Resolve 'ref' field by merging parent config into current.
    Returns resolved config dict.
    """
    if "ref" not in cfg:
        return cfg.copy()

    visited = []
    current = cfg.copy()
    resolving_name = cfg.get("name", "<unnamed>")

    while "ref" in current:
        ref_name = current["ref"]

        if ref_name in visited:
            cycle_path = " -> ".join([resolving_name] + visited + [ref_name])
            raise ConfigError(f"Circular reference detected: {cycle_path}")

        if ref_name not in raw_configs:
            raise ConfigError(
                f"Reference '{ref_name}' not found for '{resolving_name}'"
            )

        parent_base = raw_configs[ref_name].copy()
        child_overrides = current.copy()
        del child_overrides["ref"]

        parent_base.update(child_overrides)
        current = parent_base
        visited.append(ref_name)
        resolving_name = ref_name

    return current


def load_repo_config(config_path: Path) -> tuple[list[dict], dict]:
    """
    Load and validate repository configuration from file.
    Returns (repos_list, global_cfg).
    """
    global BASE_DIR
    if BASE_DIR is None:
        raise ConfigError("BASE_DIR not set. Call set_base_dir() first.")

    cp = configparser.ConfigParser()
    if not config_path.exists():
        colors.error(f"Error: Config file {config_path} not found.")
        sys.exit(1)

    cp.read(config_path)

    global_cfg = {}
    if "una" in cp.sections():
        global_cfg = dict(cp["una"])
        cp.remove_section("una")

    raw_configs = {}
    for section in cp.sections():
        raw_configs[section] = dict(cp[section])

    repo_files = []
    if "repos" in global_cfg:
        repo_files = [
            r.strip()
            for r in global_cfg["repos"].replace("\\", " ").split()
            if r.strip()
        ]
    else:
        repo_files = [
            str(p.relative_to(BASE_DIR))
            for p in (BASE_DIR / "confs" / "repos").glob("*.repo")
        ]

    for r_file in repo_files:
        r_path = BASE_DIR / r_file
        if r_path.exists():
            rcp = configparser.ConfigParser()
            rcp.read(r_path)
            for section in rcp.sections():
                raw_configs[section] = dict(rcp[section])
        else:
            colors.warn(f"Warning: Repo config {r_path} not found.")

    cycle = detect_circular_refs(raw_configs)
    if cycle:
        raise ConfigError(f"Circular reference detected: {cycle}")

    final_repos = []
    for name in raw_configs:
        config = resolve_ref(raw_configs[name], raw_configs)
        config["name"] = name

        if "sparse_ignore_dirs" in config:
            config["sparse_ignore_dirs"] = [
                s.strip() for s in config["sparse_ignore_dirs"].split(",") if s.strip()
            ]
        else:
            config["sparse_ignore_dirs"] = []

        if "repo_dir" in config:
            rd = Path(config["repo_dir"])
            if not rd.is_absolute():
                config["repo_dir"] = BASE_DIR / rd
            else:
                config["repo_dir"] = rd

        kimg = {}
        for key in list(config.keys()):
            if key.startswith("kernel_image."):
                arch = key.split(".", 1)[1]
                kimg[arch] = config[key]
                del config[key]
        if kimg:
            config["kernel_image"] = kimg

        final_repos.append(config)

    for cfg in final_repos:
        if "depends" in cfg:
            cfg["depends"] = [
                s.strip() for s in cfg["depends"].replace(",", " ").split() if s.strip()
            ]
        else:
            cfg["depends"] = []

    all_repo_names = {r["name"] for r in final_repos}
    for cfg in final_repos:
        errors = validate_repo_config(cfg, all_repo_names, set())
        for err in errors:
            colors.error(err)
        if errors:
            sys.exit(1)

    seen_names = set()
    duplicates = []
    for cfg in final_repos:
        name = cfg["name"]
        if name in seen_names:
            duplicates.append(name)
        seen_names.add(name)
    if duplicates:
        raise ConfigError(f"Duplicate repo names found: {', '.join(duplicates)}")

    return final_repos, global_cfg


def deduplicate_repos(repos_config: list) -> list:
    """
    Deduplicate repos by (repo_dir, una_file) key, preserving first occurrence.
    Virtual repos are always kept.
    """
    seen_dirs = set()
    deduped = []
    for r in repos_config:
        if r.get("is_virtual"):
            deduped.append(r)
        elif "repo_dir" not in r:
            deduped.append(r)
        else:
            abs_dir = Path(r["repo_dir"]).absolute()
            dir_file_key = (abs_dir, r.get("una_file", "una.py"))
            if dir_file_key not in seen_dirs:
                deduped.append(r)
                seen_dirs.add(dir_file_key)
    return deduped


def get_transitive_deps(name: str, dep_graph: dict, visited: set = None) -> set:
    """
    Get all transitive dependencies of a repo (including indirect dependencies).

    Args:
        name: Repository name
        dep_graph: Dependency graph (name -> list of deps)
        visited: Set of already-visited names (used internally for recursion)

    Returns:
        Set of all transitive dependency names (excluding name itself)
    """
    if visited is None:
        visited = set()

    if name in visited or name not in dep_graph:
        return set()

    visited.add(name)
    deps = set()

    for dep in dep_graph.get(name, []):
        deps.add(dep)
        deps.update(get_transitive_deps(dep, dep_graph, visited))

    return deps


def filter_by_requested(repos_config: list, requested: set) -> list:
    """
    Filter repos to only include requested and their dependencies.
    Returns list of filtered repos.
    """
    if not requested:
        return repos_config

    # Build a dependency graph for the shared helper
    dep_graph = {r["name"]: r.get("depends", []) for r in repos_config}

    needed = set()
    for name in requested:
        needed.add(name)
        needed.update(get_transitive_deps(name, dep_graph))

    return [r for r in repos_config if r["name"] in needed]


def load_repo_state(config_path: Path):
    """Load a single repo's saved state from .una_config file."""
    if not config_path.exists():
        return None
    try:
        with open(config_path, "r") as f:
            return json.load(f)
    except Exception:
        return None


def save_repo_state(cfg: dict):
    """Saves the repository configuration to its directory for scanning."""
    repo_dir = Path(cfg["repo_dir"])
    if not repo_dir.exists():
        return

    state_file = repo_dir / ".una_config"
    serializable = cfg.copy()
    serializable["repo_dir"] = str(cfg["repo_dir"])

    with open(state_file, "w") as f:
        json.dump(serializable, f, indent=4)


def list_repos(repos, target_type=None):
    """
    Helper function to filter and print repo directories by type.
    If target_type is None, prints all.
    """
    if target_type == "target":
        filtered = [
            r
            for r in repos
            if r.get("type") != "tools"
            and not r.get("is_virtual")
            and r.get("type") != "virtual"
        ]
    else:
        filtered = [
            r
            for r in repos
            if (target_type is None or r.get("type") == target_type)
            and not r.get("is_virtual")
            and r.get("type") != "virtual"
        ]
    for r in filtered:
        script_info = f" (Script: {r.get('una_file', 'una.py')})"
        print(
            f"[{r.get('type', 'unknown')}] {r['name']} -> {r['repo_dir']}{script_info}"
        )
    return [r["name"] for r in filtered]


def scan_repos(repo_base: Path = None):
    """Scans the repo/ directory for existing repositories and their states."""
    global BASE_DIR
    if repo_base is None:
        repo_base = BASE_DIR / "repo"
    if not repo_base.exists():
        return []

    scanned = []
    for d in repo_base.iterdir():
        if d.is_dir():
            state_file = d / ".una_config"
            if state_file.exists():
                state = load_repo_state(state_file)
                if state:
                    rd = Path(state["repo_dir"])
                    if not rd.is_absolute():
                        state["repo_dir"] = BASE_DIR / rd
                    else:
                        state["repo_dir"] = rd
                    scanned.append(state)
    return scanned
