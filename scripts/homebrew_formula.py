"""Point the Homebrew formula at a PyPI release and regenerate its resources from uv.lock.

    python3 -I scripts/homebrew_formula.py <formula.rb> <version> <uv.lock>

The formula's `exclude_packages` (packages that homebrew-core formulae provide) are left out,
with the dependencies only they need.
"""
import json
import re
import sys
import time
import tomllib
import urllib.request

PACKAGE = "busy-crew"
# Dependencies that never install on macOS.
OTHER_PLATFORMS = re.compile(r"sys_platform == '(emscripten|win32|cygwin)'|platform_system == 'Windows'")
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


def dependencies(lock, excluded):
    """Every package busy-crew needs on macOS, minus the excluded ones and what only they need."""
    packages = {package["name"]: package for package in lock["package"]}
    needed, pending = {}, [PACKAGE]
    while pending:
        name = pending.pop()
        for dependency in packages[name].get("dependencies", []):
            child = dependency["name"]
            if child in excluded or child in needed or OTHER_PLATFORMS.search(dependency.get("marker", "")):
                continue
            needed[child] = packages[child]["version"]
            pending.append(child)
    return dict(sorted(needed.items()))


def update(formula, version, lock, lookup=sdist):
    excluded = set(re.search(r"exclude_packages:\s*%w\[([^\]]*)\]", formula).group(1).split())
    url, sha256 = lookup(PACKAGE, version)
    formula = re.sub(r'^  url ".*"$', f'  url "{url}"', formula, count=1, flags=re.MULTILINE)
    formula = re.sub(r'^  sha256 ".*"$', f'  sha256 "{sha256}"', formula, count=1, flags=re.MULTILINE)
    blocks = []
    for name, pinned in dependencies(lock, excluded).items():
        url, sha256 = lookup(name, pinned)
        blocks.append(f'  resource "{name}" do\n    url "{url}"\n    sha256 "{sha256}"\n  end\n\n')
    formula = RESOURCE.sub("", formula)
    return formula.replace("  def install\n", "".join(blocks) + "  def install\n", 1)


def main(path, version, lock_path):
    with open(lock_path, "rb") as source:
        lock = tomllib.load(source)
    with open(path) as source:
        formula = source.read()
    with open(path, "w") as output:
        output.write(update(formula, version, lock))


if __name__ == "__main__":
    main(*sys.argv[1:])
