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

## Developing on the Mac that runs your bar

Install your everyday copy from Homebrew (or `uv tool install`), as users do,
and develop in a separate checkout. The LaunchAgent, the hooks and the menu bar
icon then run a released version, whatever branch the checkout is on.
`busy-crew status` shows which copy runs (`version`, `install_mode`).

Most changes need nothing installed: the unit tests, `uv run busy-crew demo …`
for rendering, and for the menu bar app
`AGENT_DECK_SWIFT_TESTS=1 uv run python -m unittest tests.test_menubar`.

To try a branch on the real bar, pause the installed collector, run the branch
in the foreground, then resume. Only one collector can drive the bar at a time:

```sh
launchctl bootout gui/$UID/local.busybar.agent-deck   # pause the installed collector
uv run busy-crew run                                  # this branch, in the foreground; Ctrl-C to stop
uv run busy-crew snapshot /tmp/front.png --leds       # from another terminal: what the bar shows
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/local.busybar.agent-deck.plist   # back to the installed copy
```

The foreground collector uses the same state folder and configuration, and
reads the events of the installed hooks. A change to the hooks, the installer
or the menu bar app needs the branch installed for a while instead:

```sh
uv run busy-crew install           # the LaunchAgent and hooks now run this checkout
uv run busy-crew menubar install   # only for a change to the Swift app
# … try it …
busy-crew install && busy-crew menubar install   # back to the Homebrew copy
```

Each `install` replaces the previous copy's hooks and keeps the settings. While
a checkout is installed, a `git switch` there changes what runs at the next
reload: keep that checkout on the branch you are testing, and work in other
worktrees. Changes reach the Homebrew copy only through a release.

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

Before the first release, set up:

- the `pypi` environment in this repository, with required reviewers;
- a PyPI trusted publisher for `busy-crew`: owner `cmdai-hq`, repository
  `busy-crew`, workflow `release.yml`, environment `pypi`;
- a `HOMEBREW_TAP_TOKEN` secret: a fine-grained token limited to
  `cmdai-hq/homebrew-tap`, with read and write access to contents and pull
  requests;
- on `cmdai-hq/homebrew-tap`: **Allow auto-merge** in the settings, and a
  branch rule on `main` that requires the `test-bot (macos-latest)` check.
  Without the required check, auto-merge does not wait for `brew test-bot`.

Re-running a failed Release job is safe: the tap step reuses its branch and
pull request, and stops if the tap already has the version.
