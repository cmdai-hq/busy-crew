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
`docs/` are real captures (`uv run busy-crew snapshot --leds`); do not overwrite them
with fictional previews.

CI tests Linux and macOS on Python 3.11 and 3.14. Device integration and real
LaunchAgent lifecycle checks require an explicitly chosen test device/account;
unit tests and wheel smoke tests do not establish those results.
