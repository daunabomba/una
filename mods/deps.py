"""
Dependency resolution for una build system.
"""

import graphlib
from pathlib import Path

from mods import colors
from mods.config import ConfigError


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

    def add_deps(name: str, visited: set):
        if name in visited:
            return
        visited.add(name)
        if name in graph:
            needed.add(name)
            for dep in graph.get(name, []):
                add_deps(dep, visited)

    for name in required:
        add_deps(name, set())

    to_remove = set(graph.keys()) - needed
    for name in to_remove:
        del graph[name]


def get_keep_dirs(repos: list, dep_graph: dict, config_components: set) -> set:
    """
    Calculate set of repo directory paths to keep based on config.

    Keeps:
    - All repos in config_components
    - Their dependencies
    - All tools repos

    Args:
        repos: List of all repo configs
        dep_graph: Dependency graph
        config_components: Set of component names from config

    Returns:
        Set of absolute Path objects for repo directories to keep
    """
    keep = set()

    name_map = {r["name"]: r for r in repos}

    for name in config_components:
        if name not in name_map:
            continue
        cfg = name_map[name]
        if "repo_dir" in cfg:
            keep.add(Path(cfg["repo_dir"]).absolute())

        for dep in dep_graph.get(name, []):
            dep_cfg = name_map.get(dep)
            if dep_cfg and "repo_dir" in dep_cfg:
                keep.add(Path(dep_cfg["repo_dir"]).absolute())

    for r in repos:
        if r.get("type") == "tools" and "repo_dir" in r:
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
