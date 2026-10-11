# Contributing

Forks, bug reports, and pull requests are welcome. By submitting a
contribution, you agree to license it under the project's [MIT License](LICENSE).
You retain your copyright and must have the right to submit the contribution
under these terms.
Keep any required third-party license and copyright notices intact.

Use Python 3.11 or newer and [uv](https://docs.astral.sh/uv/):

```sh
uv sync --locked
uv run python -m unittest discover -s tests -v
uv build
```

Tests must use temporary directories and synthetic Claude/Codex data. Mock
launchctl, processes, network requests and the device. Do not install hooks,
restart the user's service, modify real agent databases or contact a BUSY Bar
from tests. Keep conversation contents, IDs, local paths, API tokens and runtime
files out of commits, issue attachments and test fixtures.

For changes to an adapter, include a regression covering its observable result
and failure behavior. Record the tested source version and distinguish internal
file formats from supported public APIs. For render changes, generate the
fictional previews with `uv run busy-crew demo /tmp/busy-crew-demo` and inspect
them at the native 72 × 16 resolution as well as enlarged. The README images in
`docs/` are real captures (for example `uv run busy-crew snapshot docs/tokens-today.png --leds`); do not overwrite them
with fictional previews.

CI tests Linux and macOS on Python 3.11, 3.14 and 3.15. Device integration and real
LaunchAgent lifecycle checks require an explicitly chosen test device/account;
unit tests and wheel smoke tests do not establish those results.

## Releasing

1. In a pull request, bump `version` in `pyproject.toml`, run `uv lock`, and
   move the CHANGELOG's "Unreleased" notes under the new version.
2. Merge it, then publish a GitHub release whose tag is `v` plus that version
   (`gh release create v1.2.3 --generate-notes` or the web form).
3. The Release workflow does the rest. It checks that the tag matches the
   version, tests, builds and publishes to PyPI. Then it opens a pull request on
   [cmdai-hq/homebrew-tap](https://github.com/cmdai-hq/homebrew-tap) that points
   the formula at the new sdist and regenerates its resources from `uv.lock`.
   That pull request merges itself once `brew test-bot` passes.
4. Installed copies see the release within a day (see
   [docs/privacy.md](docs/privacy.md)) and offer **Update to X** in the menu
   bar icon.

The workflow needs the `pypi` environment (with required reviewers), a PyPI
trusted publisher for `release.yml`, and a `HOMEBREW_TAP_TOKEN` secret: a
fine-grained token limited to `cmdai-hq/homebrew-tap` with read and write
access to contents and pull requests.
