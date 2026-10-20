# Wine 11.16 recovery (2026-10-20)

The custom Wine path in `~/.xlcore/launcher.ini` was garbage-collected when
nixway's flake.lock moved `nixpkgs-unstable` from the 11.16 rev to 11.19.

## Root cause

`~/Projects/nixway/modules/home/gaming/default.nix:62` builds
`wine = pkgs-unstable.wineWow64Packages.staging` plus a `symlinkJoin` wrapper
(NTsync via `WINEFSYNC=0`/`WINEESYNC=0`, `d3dcompiler_47=n,b`, `wine64` symlink).
The wrapper lives only in the home-manager generation — no GC root of its own.
When `nixpkgs-unstable` moved past 11.16, the old closure was collected.

## Recovery

- 11.16 rev: `e158d9ed9b51c98974c5e66e1ba1c9e0255fecaa`
  (found by `nix eval ...#wineWow64Packages.staging.version` across
  `git -C ~/Projects/nixway log -- flake.lock` history)
- Rebuilt: `nix build github:NixOS/nixpkgs/<rev>#wineWow64Packages.staging`
  plus the `symlinkJoin` wrapper from `default.nix:70-85`
- Result: `/nix/store/79fjvrvj02rji96q7b1ag93h7y0pv6wk-wine-staging-11.16-xivlauncher/bin`
  (`wine-11.16 (Staging)`, verified)
- GC roots: `~/.local/share/nix-gcroots/wine-11.16-{raw,wrapped,store}`
- `~/.xlcore/launcher.ini` `WineBinaryPath` updated to the recovered path

## Prevention

Add a `nix-store --add-root` GC root (done above) or pin the wrapper output
in nixway so future `update-system` runs don't collect the working Wine.
