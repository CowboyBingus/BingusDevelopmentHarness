#!/usr/bin/env bash
# A stand-in for an installation's app/linux/hellforge-linux.sh in tests. It starts no Proton, Steam or game: it
# writes logs/play-session.json and the run's result.json as the real session runner does, waits for the harness's
# stop-observation.signal, writes the final result and exits.
set -euo pipefail
root="${INSTALL:?INSTALL is not set}"
run_id="${HD2H_FAKE_RUN_ID:-0123456789abcdef0123456789abcdef}"
run="$root/logs/sessions/$run_id"
mkdir -p "$run"

# The real runner publishes a Wine path: C:\ inside its own prefix, Z:\ outside it.
windows_run="Z:${run//\//\\}"
case "$run" in
  */pfx/drive_c/*)
    windows_run="C:\\${run#*/pfx/drive_c/}"
    windows_run="${windows_run//\//\\}"
    ;;
esac
escaped_run="${windows_run//\\/\\\\}"

printf '{"runId": "%s", "run": "%s"}\n' "$run_id" "$escaped_run" > "$root/logs/play-session.json"
printf '{"status": "observing"}\n' > "$run/result.json"

trap 'exit 143' TERM INT
if [ "${HD2H_FAKE_CHILD:-0}" = 1 ]; then
  sleep 300 &
  printf '%s\n' "$!" > "$root/logs/fake-child.pid"
fi
if [ "${HD2H_FAKE_IGNORE_STOP:-0}" = 1 ]; then
  while :; do sleep 0.05; done
fi
while [ ! -f "$run/stop-observation.signal" ]; do sleep 0.05; done
printf '{"status": "observed", "gameExitCode": 0, "completedUtc": "2026-01-01T00:00:00Z"}\n' > "$run/result.json"
