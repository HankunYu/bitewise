#!/usr/bin/env bash
# Render bitewise@.service with this repo's absolute path and install it as a user unit.
# Re-run after moving the repo.
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
unit_name="bitewise@.service"

if [[ "$repo_dir" == *"|"* ]]; then
    echo "Repo path must not contain '|': $repo_dir" >&2
    exit 1
fi

mkdir -p "$unit_dir"
# Drop any symlink left by a previous `systemctl --user link` before writing a real file
rm -f "$unit_dir/$unit_name"
sed "s|@BITEWISE_HOME@|$repo_dir|g" "$repo_dir/systemd/$unit_name" > "$unit_dir/$unit_name"
systemctl --user daemon-reload

echo "Installed $unit_dir/$unit_name (BITEWISE_HOME=$repo_dir)"
