#!/usr/bin/env bash
# Builds harness_input.dll beside hellforge-mod.json (Zig, the SDK of a HellForge-Open checkout).
# Usage: tools/test-input/build.sh  (HELLFORGE_OPEN, HELLFORGE_ZIG as the mods' builds take them)
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
sdk=${HELLFORGE_OPEN:-$HOME/HellForge}/native/mod-sdk
zig=${HELLFORGE_ZIG:-$HOME/.local/share/zig-native/zig}
export ZIG_GLOBAL_CACHE_DIR=$here/.zig-cache ZIG_LOCAL_CACHE_DIR=$here/.zig-cache
"$zig" cc -std=c11 -target x86_64-windows-gnu -shared -O2 -Wall -Werror -Wno-unused-parameter -I "$sdk" \
    "$here/harness_input.c" -o "$here/harness_input.dll"
rm -f "$here"/*.lib "$here"/*.pdb
echo "Built $here/harness_input.dll"
