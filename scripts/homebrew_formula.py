"""Point the Homebrew formula at a PyPI release and regenerate its resources from uv.lock.

    uv run --locked python scripts/homebrew_formula.py <formula.rb> <version> <uv.lock>

The formula's `exclude_packages` (packages that homebrew-core formulae provide) are left out,
with the dependencies only they need. Dependency markers are evaluated for macOS on both
architectures, with the formula's `python@3.x`.
"""
import json
import re
import sys
import time
import tomllib
import urllib.request

from packaging.markers import Marker

PACKAGE = "busy-crew"
RESOURCE = re.compile(r'^  resource "[^"]+" do\n(?:    .*\n)*?  end\n\n?', re.MULTILINE)


def sdist(name, version, retries=10):
    """URL and SHA-256 of a release's sdist; a fresh upload can take a moment to appear."""
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30) as response:
                files = json.load(response)["urls"]
            source = next(item for item in files if item["packagetype"] == "sdist")
            return source["url"], source["digests"]["sha256"]
        except (OSError, StopIteration, KeyError):
            if attempt == retries - 1:
                raise
            time.sleep(30)


def environments(python):
    """Marker environments a Homebrew install can run in: macOS, either architecture, `python` as `3.x`."""
    base = {"sys_platform": "darwin", "platform_system": "Darwin", "os_name": "posix",
            "implementation_name": "cpython", "platform_python_implementation": "CPython",
            "python_version": python, "python_full_version": python + ".0"}
    return [dict(base, platform_machine=machine) for machine in ("arm64", "x86_64")]


def dependencies(lock, excluded, python):
    """Every package busy-crew needs on macOS, minus the excluded ones and what only they need."""
    packages = {package["name"]: package for package in lock["package"]}
    targets = environments(python)
    needed, pending = {}, [PACKAGE]
    while pending:
        name = pending.pop()
        for dependency in packages[name].get("dependencies", []):
            child = dependency["name"]
            marker = dependency.get("marker")
            if marker and not any(Marker(marker).evaluate(target) for target in targets):
                continue
            if child in excluded or child in needed:
                continue
            needed[child] = packages[child]["version"]
            pending.append(child)
    return dict(sorted(needed.items()))


def update(formula, version, lock, lookup=None):
    lookup = lookup or sdist
    excluded = set(re.search(r"exclude_packages:\s*%w\[([^\]]*)\]", formula).group(1).split())
    python = re.search(r'depends_on "python@(3\.\d+)"', formula).group(1)
    url, sha256 = lookup(PACKAGE, version)
    formula = re.sub(r'^  url ".*"$', f'  url "{url}"', formula, count=1, flags=re.MULTILINE)
    formula = re.sub(r'^  sha256 ".*"$', f'  sha256 "{sha256}"', formula, count=1, flags=re.MULTILINE)
    blocks = []
    for name, pinned in dependencies(lock, excluded, python).items():
        url, sha256 = lookup(name, pinned)
        blocks.append(f'  resource "{name}" do\n    url "{url}"\n    sha256 "{sha256}"\n  end\n\n')
    formula = RESOURCE.sub("", formula)
    return formula.replace("  def install\n", "".join(blocks) + "  def install\n", 1)


def main(path, version, lock_path):
    with open(lock_path, "rb") as source:
        lock = tomllib.load(source)
    with open(path) as source:
        formula = source.read()
    updated = update(formula, version, lock)  # Every PyPI lookup succeeds before the file is touched.
    with open(path, "w") as output:
        output.write(updated)


if __name__ == "__main__":
    main(*sys.argv[1:])
