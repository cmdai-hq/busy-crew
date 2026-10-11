"""Check the built wheel and sdist before they are published: `python3 -I tests/dist_contents.py dist`."""
import email
import re
import sys
import tarfile
import zipfile
from pathlib import Path

SWIFT = "agentdeck/menubar/BusyCrewMenu.swift"
# Relative links resolve against the PyPI page, where the repository files do not exist.
RELATIVE = re.compile(r'\]\((?!https?://|#)[^)]*\)|src="(?!https?://)[^"]*"')


def check_metadata(raw, source):
    metadata = email.message_from_bytes(raw)
    urls = {url.split(",", 1)[0] for url in metadata.get_all("Project-URL") or []}
    missing = {"Homepage", "Issues", "Changelog"} - urls
    if missing:
        raise RuntimeError(f"{source}: missing project URLs {sorted(missing)}")
    readme = metadata.get_payload()
    if "# Busy Crew" not in readme:
        raise RuntimeError(f"{source}: the README is not the long description")
    if links := RELATIVE.findall(readme):
        raise RuntimeError(f"{source}: README links that break on PyPI: {links}")


def main(directory):
    wheels, sdists = list(directory.glob("*.whl")), list(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise RuntimeError(f"Expected one wheel and one sdist in {directory}")
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())
        info = next(name.split("/")[0] for name in names if name.endswith(".dist-info/METADATA"))
        for required in (SWIFT, f"{info}/licenses/LICENSE"):
            if required not in names:
                raise RuntimeError(f"Wheel is missing {required}")
        check_metadata(wheel.read(f"{info}/METADATA"), "wheel")
    with tarfile.open(sdists[0]) as sdist:
        root = sdist.getnames()[0].split("/")[0]
        names = {name.removeprefix(root + "/") for name in sdist.getnames()}
        for required in (SWIFT, "LICENSE", "README.md", "CHANGELOG.md", "pyproject.toml"):
            if required not in names:
                raise RuntimeError(f"Sdist is missing {required}")
        check_metadata(sdist.extractfile(f"{root}/PKG-INFO").read(), "sdist")
    print(f"{wheels[0].name} and {sdists[0].name} contain the app source, license and README")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
