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
  depends_on "python@3.15"

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
        {"name": "idna"}, {"name": "httpx2-jsfetch", "marker": "sys_platform == 'emscripten'"},
        {"name": "linux-only", "marker": "sys_platform == 'linux'"},
        # Mixed, as in the real uv.lock: applies on macOS before Python 3.15 only.
        {"name": "older-pythons", "marker": "(python_full_version < '3.15' and sys_platform != 'emscripten') or "
                                            "(python_full_version < '3.12' and sys_platform == 'emscripten')"},
        {"name": "intel-only", "marker": "platform_machine == 'x86_64'"}]},
    {"name": "linux-only", "version": "1.0"},
    {"name": "older-pythons", "version": "1.0"},
    {"name": "intel-only", "version": "1.0"},
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
        self.assertEqual(resources, ["test-image", "busylib", "httpx2", "idna", "intel-only", "pillow",
                                     "typing-extensions"])
        self.assertNotIn("removed-dependency", updated)
        self.assertIn('    url "https://files.pythonhosted.org/idna-3.20.tar.gz"\n    sha256 "sha-idna-3.20"\n', updated)
        self.assertLess(updated.index('resource "typing-extensions"'), updated.index("  def install"))

    def test_markers_are_evaluated_for_the_formula_python(self):
        older = FORMULA.replace('depends_on "python@3.15"', 'depends_on "python@3.14"')
        self.assertIn('resource "older-pythons"', homebrew_formula.update(older, "1.1.0", LOCK, lookup))
        self.assertNotIn('resource "older-pythons"', homebrew_formula.update(FORMULA, "1.1.0", LOCK, lookup))

    def test_failed_lookup_leaves_the_formula_untouched(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            formula, lock = Path(directory) / "busy-crew.rb", Path(directory) / "uv.lock"
            formula.write_text(FORMULA)
            lock.write_text('[[package]]\nname = "busy-crew"\nversion = "1.1.0"\n')
            with patch.object(homebrew_formula, "sdist", side_effect=OSError("PyPI unavailable")):
                with self.assertRaises(OSError):
                    homebrew_formula.main(formula, "1.1.0", lock)
            self.assertEqual(formula.read_text(), FORMULA)

    def test_updating_twice_changes_nothing(self):
        once = homebrew_formula.update(FORMULA, "1.1.0", LOCK, lookup)
        self.assertEqual(homebrew_formula.update(once, "1.1.0", LOCK, lookup), once)

    def test_a_new_version_drops_the_previous_bottles(self):
        bottled = homebrew_formula.update(FORMULA, "1.1.0", LOCK, lookup).replace(
            '  license "MIT"\n', '  license "MIT"\n\n  bottle do\n'
            '    root_url "https://github.com/cmdai-hq/busy-crew/releases/download/v1.1.0"\n'
            '    sha256 cellar: :any, arm64_sequoia: "bottle-1.1.0"\n  end\n')
        self.assertEqual(homebrew_formula.update(bottled, "1.1.0", LOCK, lookup), bottled)
        updated = homebrew_formula.update(bottled, "1.1.1", LOCK, lookup)
        self.assertNotIn("bottle", updated)
        self.assertIn('  sha256 "sha-busy-crew-1.1.1"\n  license "MIT"\n\n  depends_on "pydantic"\n', updated)

    def test_real_formula_dependencies_skip_what_homebrew_provides(self):
        import tomllib
        with open(Path(__file__).parents[1] / "uv.lock", "rb") as source:
            lock = tomllib.load(source)
        needed = homebrew_formula.dependencies(lock, {"pillow", "pydantic", "pydantic-core"}, "3.15")
        self.assertIn("busylib", needed)
        self.assertIn("pydantic-settings", needed)
        for provided in ["pillow", "pydantic", "pydantic-core", "httpx2-jsfetch", "busy-crew"]:
            self.assertNotIn(provided, needed)


if __name__ == "__main__":
    unittest.main()
