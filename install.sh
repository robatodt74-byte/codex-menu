#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
source_app="$PWD/build/CodexMenu.app"
target_app="$HOME/Applications/CodexMenu.app"
if [ -e "$target_app" ]; then
  echo "Existing app found; refusing to overwrite: $target_app" >&2
  exit 1
fi
if [ ! -d "$source_app" ]; then bash build.sh; fi
mkdir -p "$HOME/Applications"
# The workspace's file provider adds Finder metadata to .app bundles. Deploy
# only the generated bundle to the ordinary local Applications directory.
/usr/bin/ditto --norsrc --noextattr "$source_app" "$target_app"
/usr/bin/xattr -cr "$target_app"
/usr/bin/codesign --verify --deep --strict "$target_app"
printf 'Installed: %s\n' "$target_app"
