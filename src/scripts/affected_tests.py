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
    """Known modules that ``path`` imports or names in a string literal."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return set()
    is_pkg = path.name == "__init__.py"
    found: set[str] = set()

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
            if "." in value and " " not in value and len(value) < 200:
                head = value.split(".", 1)[0]
                if head in {m.split(".", 1)[0] for m in known}:
                    add(value)
    found.discard(module)
    return found


def changed_files(base: str) -> list[str]:
    def git(*args: str) -> list[str]:
        out = subprocess.run(
            ["git", *args], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout
        return [line for line in out.splitlines() if line]

    committed = git("diff", "--name-only", f"{base}...HEAD")
    uncommitted = git("diff", "--name-only", "HEAD")
    untracked = git("ls-files", "--others", "--exclude-standard")
    return sorted(set(committed + uncommitted + untracked))


def select(changed: list[str], depth: int = 0) -> list[str]:
    files = _python_files()
    known = set(files)
    if SELECT_ALL & set(changed):
        return sorted(str(p.relative_to(REPO)) for m, p in files.items() if _is_test(p))

    importers: dict[str, set[str]] = defaultdict(set)
    for mod, path in files.items():
        for dep in _imports(path, mod, known):
            importers[dep].add(mod)

    path_to_mod = {str(p.relative_to(REPO)): m for m, p in files.items()}
    seeds = {path_to_mod[c] for c in changed if c in path_to_mod}

    # Walk importers breadth-first. Tests are collected at every level;
    # only non-test modules are expanded, at most ``depth`` hops past the
    # changed modules (depth < 0 means unlimited).
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
                if not _is_test(files[user]):
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
                text = path.read_text(encoding="utf-8", errors="ignore")
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
        print("no affected tests", file=sys.stderr)
        return 0
    cmd = [sys.executable, "-m", "pytest", *tests, *args.pytest_args]
    return subprocess.call(cmd, cwd=REPO)


if __name__ == "__main__":
    raise SystemExit(main())
