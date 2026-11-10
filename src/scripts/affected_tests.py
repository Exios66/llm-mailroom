"""Select the test files a change can affect, so a task runs only those.

The full suite takes over ten minutes. Most changes touch a few modules,
and only the tests that import them (directly or transitively) can see the
change. This script builds the import graph of ``src/`` with ``ast`` (no
imports are executed) and walks it backwards from the changed files.

A test file is selected when any of these holds:

- the test file itself changed;
- it imports a changed module directly, or through at most ``--depth``
  other ``src`` modules (default 0; ``-1`` follows every import);
- it names a changed module in a string (``monkeypatch.setattr("pkg.mod.x")``,
  ``importlib.import_module("pkg.mod")``);
- a changed non-Python file (YAML, Markdown, fixtures) is named in its
  source by file name.

A change to ``conftest.py`` or ``pyproject.toml`` selects every test.

Usage::

    PYTHONPATH=src python src/scripts/affected_tests.py                 # vs origin/main, incl. uncommitted
    PYTHONPATH=src python src/scripts/affected_tests.py --base HEAD~1   # one commit
    PYTHONPATH=src python src/scripts/affected_tests.py --depth 1       # also tests of direct importers
    PYTHONPATH=src python src/scripts/affected_tests.py --run -- -q     # run pytest on the selection
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
TESTS = SRC / "tests"
SELECT_ALL = {"src/tests/conftest.py", "pyproject.toml"}


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _python_files() -> dict[str, Path]:
    return {
        _module_name(p): p
        for p in SRC.rglob("*.py")
        if "egg-info" not in p.parts and "__pycache__" not in p.parts
    }


def _imports(path: Path, module: str, known: set[str]) -> set[str]:
    """Known modules imported, named dynamically, or executed by literal path."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return set()
    is_pkg = path.name == "__init__.py"
    found: set[str] = set()
    roots = {m.split(".", 1)[0] for m in known}
    by_stem: dict[str, set[str]] = defaultdict(set)
    for name in known:
        by_stem[name.rsplit(".", 1)[-1]].add(name)

    def add(name: str) -> None:
        # "pkg.mod.attr" -> the longest known prefix; also every parent
        # package, since importing pkg.mod executes pkg/__init__.py.
        parts = name.split(".")
        for i in range(len(parts), 0, -1):
            cand = ".".join(parts[:i])
            if cand in known:
                found.add(cand)
                break
        for i in range(1, len(parts)):
            parent = ".".join(parts[:i])
            if parent in known:
                found.add(parent)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = module.split(".")
                base = base if is_pkg else base[:-1]
                base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                prefix = ".".join(base)
                target = f"{prefix}.{node.module}" if node.module else prefix
            else:
                target = node.module or ""
            add(target)
            for alias in node.names:
                add(f"{target}.{alias.name}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            # CLI consumers often execute a source file rather than import
            # its module. A literal basename also covers Path("src") / ...
            # constructions; collisions conservatively include every match.
            if value.endswith(".py"):
                stem = Path(value).stem
                for candidate in by_stem.get(stem, ()):
                    add(candidate)
            if "." in value and " " not in value and len(value) < 200:
                head = value.split(".", 1)[0]
                if head in roots:
                    add(value)
    found.discard(module)
    return found


def changed_files(base: str) -> list[str]:
    def git(*args: str) -> list[str]:
        out = subprocess.run(
            ["git", *args], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout
        return [name for name in out.split("\0") if name]

    committed = git("diff", "--no-renames", "--name-only", "-z", f"{base}...HEAD")
    uncommitted = git("diff", "--no-renames", "--name-only", "-z", "HEAD")
    untracked = git("ls-files", "--others", "--exclude-standard", "-z")
    return sorted(set(committed + uncommitted + untracked))


class Index:
    """The import graph of ``src/``: build once, then select for any number of changes."""

    def __init__(self) -> None:
        """Parse every ``src`` module and invert the import edges (``importers[dep]``)."""
        self.files = _python_files()
        known = set(self.files)
        self.importers: dict[str, set[str]] = defaultdict(set)
        for mod, path in self.files.items():
            for dep in _imports(path, mod, known):
                self.importers[dep].add(mod)
        self.path_to_mod = {str(p.relative_to(REPO)): m for m, p in self.files.items()}
        self._test_text: dict[str, str] = {}

    def test_text(self, mod: str) -> str:
        """Source text of a test module, read once."""
        if mod not in self._test_text:
            self._test_text[mod] = self.files[mod].read_text(encoding="utf-8", errors="ignore")
        return self._test_text[mod]


def select(changed: list[str], depth: int = 0, index: Index | None = None) -> list[str]:
    """Test files that can see ``changed``; pass a shared ``index`` to avoid re-parsing ``src``."""
    index = index or Index()
    files = index.files
    if SELECT_ALL & set(changed):
        return sorted(str(p.relative_to(REPO)) for m, p in files.items() if _is_test(p))

    importers = index.importers
    seeds = {index.path_to_mod[c] for c in changed if c in index.path_to_mod}

    # Walk importers breadth-first. Tests are collected at every level;
    # bounded local mode expands non-test modules up to ``depth`` hops.
    # Full closure also follows re-exported/shared test helpers.
    affected = set(seeds)
    frontier = set(seeds)
    hop = 0
    while frontier:
        nxt: set[str] = set()
        for mod in frontier:
            for user in importers.get(mod, ()):
                if user in affected:
                    continue
                affected.add(user)
                if depth < 0 or not _is_test(files[user]):
                    nxt.add(user)
        if depth >= 0 and hop >= depth:
            break
        frontier = nxt
        hop += 1

    selected = {str(files[m].relative_to(REPO)) for m in affected if _is_test(files[m])}

    data_names = [Path(c).name for c in changed if not c.endswith(".py")]
    if data_names:
        for mod, path in files.items():
            if _is_test(path):
                text = index.test_text(mod)
                if any(name in text for name in data_names):
                    selected.add(str(path.relative_to(REPO)))
    return sorted(selected)


def _is_test(path: Path) -> bool:
    return TESTS in path.parents and path.name.startswith("test_")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="origin/main", help="git ref to diff against")
    parser.add_argument("--files", nargs="*", help="explicit changed paths (skip git)")
    parser.add_argument(
        "--depth",
        type=int,
        default=0,
        help="hops through non-test importers (0 = tests that reference a changed "
        "module directly; -1 = full transitive closure)",
    )
    parser.add_argument("--run", action="store_true", help="run pytest on the selection")
    parser.add_argument("pytest_args", nargs="*", help="extra pytest args after --")
    args = parser.parse_args(argv)

    changed = args.files if args.files is not None else changed_files(args.base)
    tests = select(changed, args.depth)
    if not args.run:
        print("\n".join(tests))
        return 0
    if not tests:
        print("no affected tests; use ci_tests.py for a safe fallback", file=sys.stderr)
        return 5
    cmd = [sys.executable, "-m", "pytest", *tests, *args.pytest_args]
    return subprocess.call(cmd, cwd=REPO)


if __name__ == "__main__":
    raise SystemExit(main())
