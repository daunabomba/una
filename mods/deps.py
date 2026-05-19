"""
Dependency resolution for una build system.
"""

import graphlib
from pathlib import Path

from mods import colors
from mods.config import ConfigError, get_transitive_deps


def build_dep_graph(repos: list) -> dict:
    """Build dependency graph from repos list."""
    return {r["name"]: r.get("depends", []) for r in repos}


def get_build_order(repos: list, required_names: set = None) -> tuple[list, dict]:
    """
    Get topological build order for repos.

    Args:
        repos: List of repo configs with 'name' and 'depends' fields
        required_names: Set of specific repos to build (None = all)

    Returns:
        (ordered_names, dep_graph) - tuple of ordered list and full graph

    Raises:
        ConfigError: If circular dependencies detected
    """
    dep_graph = build_dep_graph(repos)

    # Hardcode: linux-image depends on all other components
    # that don't depend on linux-image (to avoid circular deps)
    all_names = {r["name"] for r in repos}
    if "linux-image" in all_names:
        # Find components that depend on linux-image (directly or indirectly)
        depends_on_linux = set()

        def find_deps_on_linux(name: str, visited: set):
            if name in visited:
                return
            visited.add(name)
            for n, deps in dep_graph.items():
                if name in deps and n not in depends_on_linux:
                    depends_on_linux.add(n)
                    find_deps_on_linux(n, visited)

        find_deps_on_linux("linux-image", set())

        # linux-image depends on all except itself and those that depend on it
        other_names = [
            n for n in all_names if n != "linux-image" and n not in depends_on_linux
        ]
        dep_graph["linux-image"] = other_names

    if required_names:
        prune_graph(dep_graph, required_names)

    try:
        ts = graphlib.TopologicalSorter(dep_graph)
        ordered = list(ts.static_order())
    except graphlib.CycleError as e:
        raise ConfigError(f"Circular dependency detected: {e}")

    return ordered, dep_graph


def prune_graph(graph: dict, required: set) -> None:
    """
    Prune graph to only include required repos and their dependencies.
    Modifies graph in place.
    """
    needed = set()

    for name in required:
        needed.add(name)
        needed.update(get_transitive_deps(name, graph))

    to_remove = set(graph.keys()) - needed
    for name in to_remove:
        del graph[name]


def get_keep_dirs(repos: list, dep_graph: dict) -> set:
    """
    Calculate set of repo directory paths to keep based on pruned dependency graph.

    Keeps:
    - All repos present in the pruned dependency graph (includes all transitive dependencies)
    - All tools repos

    Args:
        repos: List of all repo configs
        dep_graph: Pruned dependency graph from get_build_order()

    Returns:
        Set of absolute Path objects for repo directories to keep
    """
    keep = set()
    name_map = {r["name"]: r for r in repos}

    # Add all repos in the pruned dependency graph (already includes transitive deps)
    for name in dep_graph:
        cfg = name_map.get(name)
        if cfg and "repo_dir" in cfg:
            keep.add(Path(cfg["repo_dir"]).absolute())

    # Keep tools repos only if they appear in the pruned dependency graph
    # (i.e., they are required by top-level config or are transitive dependencies).
    for r in repos:
        if r.get("type") == "tools" and r["name"] in dep_graph and "repo_dir" in r:
            keep.add(Path(r["repo_dir"]).absolute())

    return keep


def filter_repos_for_build(repos: list) -> list:
    """Filter repos to non-virtual, non-tools repos with repo_dir."""
    return [
        r
        for r in repos
        if not r.get("is_virtual")
        and r.get("type") != "virtual"
        and r.get("type") != "tools"
        and "repo_dir" in r
    ]


def filter_repos_for_sync(repos: list) -> list:
    """Filter repos to non-virtual repos (for sync operations)."""
    return [r for r in repos if not r.get("is_virtual")]


def get_sync_set(dep_graph: dict, repos: list) -> set:
    """
    Compute the set of repository names that need to be synced.

    Includes:
    - All names present in the (pruned) dependency graph.
    - Any 'tools' type repositories that are referenced (directly or transitively)
      by entries in the pruned dependency graph.
    """
    name_map = {r["name"]: r for r in repos}
    sync = set(dep_graph.keys())

    # Check direct and transitive dependencies for referenced tools.
    for name in list(dep_graph.keys()):
        # gather transitive deps using helper (works with pruned graph)
        try:
            deps = get_transitive_deps(name, dep_graph)
        except Exception:
            deps = set(dep_graph.get(name, []))
        # include direct deps as well
        deps.update(dep_graph.get(name, []))
        for dep in deps:
            dep_cfg = name_map.get(dep)
            if dep_cfg and dep_cfg.get("type") == "tools":
                sync.add(dep)

    return sync
