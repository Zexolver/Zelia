# Packaging

ZELIA ships as an Arch/Manjaro pacman package (`.pkg.tar.zst`) rather than
a manual install script -- `makepkg`'s default compression is zstd, so
building either PKGBUILD below with a stock `makepkg.conf` already produces
`.pkg.tar.zst` with no extra flags needed. (No `.deb` -- this project only
targets Arch-based distros.)

Two build variants, matching stable releases vs. day-to-day
debugging/testing:

- **`stable/PKGBUILD`** -- builds package `zelia` from a tagged release
  (`v${pkgver}` on GitHub). This is what a normal user installs.
- **`testing/PKGBUILD`** -- builds package `zelia-git`, always tracking the
  latest commit on `main`, version auto-derived from `git describe`
  (standard Arch `-git` package convention). Use this to test a change
  before cutting a release. `zelia` and `zelia-git` conflict with each
  other -- only one is installed at a time.

## Building

```bash
cd packaging/stable    # or packaging/testing
makepkg -si            # build + install; drop -i to just build
```

Output lands next to the PKGBUILD as `<pkgname>-<pkgver>-<pkgrel>-x86_64.pkg.tar.zst`.

Cutting a stable release: bump `pkgver`/`pkgrel` in `packaging/stable/PKGBUILD`,
tag the repo `v<pkgver>`, push the tag, then build as above.

## What's actually in the package

Both variants install identically:

- `/opt/zelia` -- `src/`, `config/config.yaml.template`, `systemd/*.template`,
  and a bundled venv with `requirements.txt` already installed. Owned by
  pacman, read-only, shared across users on the machine.
- `/usr/bin/zelia-setup` -- per-user first-run script. `/opt/zelia` has no
  idea which user(s) will actually run ZELIA, so anything user-specific
  (a `~/.zelia` data dir, a rendered `config.yaml`, the systemd `--user`
  service, `ollama pull`, `input` group membership for ydotool) happens
  here instead of at package-install time. Run it once, as yourself, after
  installing the package.
- `/usr/bin/zelia-say` -- typed input from any terminal
  (`src/text_repl.py`), any time, independent of the service.

This split (system packages + shared code via pacman, per-user data/config
via `zelia-setup`) replaces what `install.sh` used to do in one shot at the
repo root. `install.sh` still exists for a from-source/dev setup, but the
package is the intended path for actually running ZELIA day to day.
