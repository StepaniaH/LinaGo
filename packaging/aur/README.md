# AUR packaging

`PKGBUILD` builds LinaGo from a GitHub `v*` tag. It is maintained here
and published to the AUR on each release.

## Release checklist (Arch machine required)

```bash
# 1. Fill in checksums for the new tag
updpkgsums

# 2. Build locally and sanity-check
makepkg -si
namcap PKGBUILD

# 3. Regenerate metadata
makepkg --printsrcinfo > .SRCINFO

# 4. Publish / update the AUR repository
git clone ssh://aur@aur.archlinux.org/linago.git aur-linago   # first time
cp PKGBUILD .SRCINFO aur-linago/
cd aur-linago && git add . && git commit -m "update to $pkgver" && git push
```

## Notes

- The GTK layer-shell linking workaround that `run.sh` applies from a
  checkout is not needed for the packaged wheel; if a system ships the
  GIR without a resolvable library, set
  `LD_PRELOAD=/usr/lib/libgtk4-layer-shell.so` for the launcher.
- `sha256sums` must be regenerated per release; the committed value is
  a placeholder by design.
