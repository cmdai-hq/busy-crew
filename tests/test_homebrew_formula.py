"""The release job's formula update; PyPI lookups are faked."""
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "homebrew_formula", Path(__file__).parents[1] / "scripts" / "homebrew_formula.py")
homebrew_formula = importlib.util.module_from_spec(spec)
spec.loader.exec_module(homebrew_formula)

FORMULA = '''class BusyCrew < Formula
  include Language::Python::Virtualenv

  url "https://github.com/cmdai-hq/busy-crew/archive/refs/tags/v1.0.1.tar.gz"
  sha256 "old"
  license "MIT"

  depends_on "pydantic"

  pypi_packages package_name:     "busy-crew",
                exclude_packages: %w[pydantic pydantic-core]

  resource "removed-dependency" do
    url "https://files.pythonhosted.org/removed-1.0.tar.gz"
    sha256 "removed"
  end

  resource "test-image", :test do
    url "https://example.invalid/image.png"
    sha256 "kept"
  end

  def install
    virtualenv_install_with_resources
  end
end
'''

LOCK = {"package": [
    {"name": "busy-crew", "version": "1.1.0", "dependencies": [{"name": "busylib"}, {"name": "pillow"}]},
    {"name": "busylib", "version": "2.6.0", "dependencies": [
        {"name": "pydantic"}, {"name": "httpx2"}, {"name": "typing-extensions"}]},
    {"name": "httpx2", "version": "2.13.1", "dependencies": [
        {"name": "idna"}, {"name": "httpx2-jsfetch", "marker": "sys_platform == 'emscripten'"}]},
    {"name": "httpx2-jsfetch", "version": "1.0"},
    {"name": "idna", "version": "3.20"},
    {"name": "pillow", "version": "12.3.0"},
    {"name": "pydantic", "version": "2.13.5", "dependencies": [{"name": "pydantic-core"}, {"name": "annotated-types"}]},
    {"name": "pydantic-core", "version": "2.46.5"},
    {"name": "annotated-types", "version": "0.8.0"},
    {"name": "typing-extensions", "version": "4.16.0"},
]}


def lookup(name, version):
    return f"https://files.pythonhosted.org/{name}-{version}.tar.gz", f"sha-{name}-{version}"


class FormulaUpdateTests(unittest.TestCase):
    def test_points_at_the_pypi_sdist_and_regenerates_resources(self):
        updated = homebrew_formula.update(FORMULA, "1.1.0", LOCK, lookup)
        self.assertIn('  url "https://files.pythonhosted.org/busy-crew-1.1.0.tar.gz"\n'
                      '  sha256 "sha-busy-crew-1.1.0"\n', updated)
        resources = [line.split('"')[1] for line in updated.splitlines() if line.startswith("  resource ")]
        # Excluded formulae and what only they need, and other platforms' packages, are left out.
        self.assertEqual(resources, ["test-image", "busylib", "httpx2", "idna", "pillow", "typing-extensions"])
        self.assertNotIn("removed-dependency", updated)
        self.assertIn('    url "https://files.pythonhosted.org/idna-3.20.tar.gz"\n    sha256 "sha-idna-3.20"\n', updated)
        self.assertLess(updated.index('resource "typing-extensions"'), updated.index("  def install"))

    def test_updating_twice_changes_nothing(self):
        once = homebrew_formula.update(FORMULA, "1.1.0", LOCK, lookup)
        self.assertEqual(homebrew_formula.update(once, "1.1.0", LOCK, lookup), once)

    def test_real_formula_dependencies_skip_what_homebrew_provides(self):
        import tomllib
        with open(Path(__file__).parents[1] / "uv.lock", "rb") as source:
            lock = tomllib.load(source)
        needed = homebrew_formula.dependencies(lock, {"pillow", "pydantic", "pydantic-core"})
        self.assertIn("busylib", needed)
        self.assertIn("pydantic-settings", needed)
        for provided in ["pillow", "pydantic", "pydantic-core", "httpx2-jsfetch", "busy-crew"]:
            self.assertNotIn(provided, needed)


if __name__ == "__main__":
    unittest.main()
