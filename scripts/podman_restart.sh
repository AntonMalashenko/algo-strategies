#!/usr/bin/env bash
# Manual restart / rebuild / cleanup for the Podman+Ofelia container stack
# (docker-compose.yml) -- run this by hand after the Mac slept, after a
# crash, or after changing code that needs to reach the running system.
#
# Deliberately separate from scripts/podman_healthcheck.py (the automated
# launchd watchdog, every 5 min via deployment/
# com.algo.podman-healthcheck.plist): that script only detects and heals
# Podman-machine clock drift / unreachability -- it never rebuilds the
# algo-worker image and never removes leftover containers. This script
# covers the two things it deliberately does NOT do, and is meant to be
# run by Anton directly, not scheduled.
#
# Why --rebuild matters: Dockerfile does `COPY . .` at build time --
# ofelia's job-run containers run FROM the built algo-worker:latest image,
# they do NOT bind-mount a live copy of the repo. A code change under
# bot/, webapp/, strategies/, utils/, scripts/ etc. has NO effect on the
# running system, no matter how many times the container is restarted,
# until the image is rebuilt.
#
# Usage:
#   scripts/podman_restart.sh              # restart the stack as-is
#   scripts/podman_restart.sh --rebuild    # rebuild algo-worker:latest first, then restart
#
# The cleanup step at the end always runs, regardless of --rebuild -- not
# an option. ofelia's job-run containers are meant to self-delete
# (ofelia.job-run.dispatch.delete=true, docker-compose.yml) but a host
# sleep, crash, or `podman machine`/container restart mid-run can strand
# one in Created/Exited/Dead state before that self-delete step runs --
# this is exactly the kind of leftover this script exists to sweep up.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

# Parsed before the podman lookup below so --help/a bad argument works
# (and fails fast) even if podman itself isn't on this PATH.
REBUILD=0
case "${1:-}" in
  --rebuild) REBUILD=1 ;;
  "") ;;
  -h|--help)
    echo "usage: $0 [--rebuild]"
    exit 0
    ;;
  *)
    echo "usage: $0 [--rebuild]" >&2
    exit 1
    ;;
esac

# Same PATH pitfall documented in scripts/podman_healthcheck.py's PODMAN
# constant (a minimal launchd environment doesn't include /opt/podman/bin)
# -- less likely from an interactive shell where this script is meant to
# be run, but cheap to guard the same way here too.
PODMAN="$(command -v podman || true)"
if [ -z "$PODMAN" ] && [ -x /opt/podman/bin/podman ]; then
  PODMAN="/opt/podman/bin/podman"
fi
if [ -z "$PODMAN" ]; then
  echo "ERROR: podman not found on PATH or at /opt/podman/bin/podman." >&2
  exit 1
fi

# Must match scripts/podman_healthcheck.py::MACHINE_NAME. There is no
# shared config file between that Python watchdog and this bash script, so
# if the machine is ever renamed, update both places by hand.
MACHINE_NAME="podman-machine-default"
WORKER_IMAGE="algo-worker:latest"

echo "=== 1/4: podman machine ($MACHINE_NAME) ==="
STATE="unknown"
if INSPECT_JSON="$("$PODMAN" machine inspect "$MACHINE_NAME" 2>/dev/null)"; then
  STATE="$(echo "$INSPECT_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0].get("State","unknown"))' 2>/dev/null || echo unknown)"
fi
if [ "$STATE" != "running" ]; then
  echo "machine state=$STATE -- starting it (the normal case right after the Mac slept or the machine crashed)"
  "$PODMAN" machine start "$MACHINE_NAME" || echo "(machine start reported an error -- it may already be starting/running; continuing)"
else
  echo "machine already running"
fi

if [ "$REBUILD" -eq 1 ]; then
  echo "=== 2/4: rebuilding $WORKER_IMAGE (code changes only take effect after this) ==="
  "$PODMAN" build -t "$WORKER_IMAGE" .
else
  echo "=== 2/4: --rebuild not passed -- reusing existing $WORKER_IMAGE, skipping build ==="
fi

echo "=== 3/4: restarting the compose stack ==="
"$PODMAN" compose up -d --force-recreate ofelia

echo "=== 4/4: cleaning up leftover $WORKER_IMAGE containers (mandatory, always runs) ==="
STALE_IDS="$("$PODMAN" ps -a \
  --filter "ancestor=$WORKER_IMAGE" \
  --filter "status=created" \
  --filter "status=exited" \
  --filter "status=dead" \
  -q)"
if [ -n "$STALE_IDS" ]; then
  echo "$STALE_IDS" | xargs "$PODMAN" rm -f
  N="$(echo "$STALE_IDS" | wc -l | tr -d ' ')"
  echo "removed $N leftover container(s)"
else
  echo "nothing to clean up"
fi

echo
echo "=== done -- current stack state ==="
"$PODMAN" compose ps
