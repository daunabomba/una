"""
Trace logging for una - logs repo and build operations to a file.
"""
from pathlib import Path
from datetime import datetime


_trace_file = None


def init_trace(filename: str) -> None:
    """Initialize trace file, overwriting if exists."""
    global _trace_file
    _trace_file = Path(filename)
    _trace_file.parent.mkdir(parents=True, exist_ok=True)
    with open(_trace_file, "w") as f:
        f.write("")
    _write("Trace started")


def is_enabled() -> bool:
    """Check if tracing is enabled."""
    return _trace_file is not None


def _write(msg: str) -> None:
    """Write a message to trace file."""
    if _trace_file:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(_trace_file, "a") as f:
            f.write(f"[{timestamp}] {msg}\n")


def trace_deps(build_order: list, dep_graph: dict) -> None:
    """Log the dependency graph and build order."""
    if not _trace_file:
        return
    _write("=== DEPENDENCY GRAPH ===")
    for name, deps in dep_graph.items():
        if deps:
            _write(f"  {name} -> depends on: {', '.join(deps)}")
        else:
            _write(f"  {name} -> (no dependencies)")
    _write("=== BUILD ORDER ===")
    for i, name in enumerate(build_order, 1):
        _write(f"  {i}. {name}")
    _write("=== END DEPENDENCY INFO ===")


def repo_created(name: str, repo_dir: Path) -> None:
    """Log repo creation."""
    _write(f"REPO CREATED: {name} -> {repo_dir}")


def repo_removed(name: str, repo_dir: Path) -> None:
    """Log repo removal."""
    _write(f"REPO REMOVED: {name} -> {repo_dir}")


def repo_synced(name: str, repo_dir: Path) -> None:
    """Log repo sync operation."""
    _write(f"REPO SYNCED: {name} -> {repo_dir}")


def build_step_start(arch: str, name: str, step: str) -> None:
    """Log build step start."""
    _write(f"BUILD START: [{arch}] {name}::{step}")


def build_step_end(arch: str, name: str, step: str) -> None:
    """Log build step end."""
    _write(f"BUILD END: [{arch}] {name}::{step}")


def tools_step_start(step: str) -> None:
    """Log tools step start."""
    _write(f"TOOLS START: {step}")


def tools_step_end(step: str) -> None:
    """Log tools step end."""
    _write(f"TOOLS END: {step}")