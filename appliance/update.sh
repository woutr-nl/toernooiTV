#!/usr/bin/env bash
# Toernooi TV self-update. Run as root by toernooitv-update.service (started on
# demand via POST /update, or: sudo systemctl start toernooitv-update).
#
#   fetch tags → newest release v X.Y.Z → checkout → restart server → health-check
#   the served version → restart kiosk. On a failed health check: checkout the
#   previous commit again, restart server + kiosk, report "rolled-back".
#
# Outcome goes to update-state.json (served in GET /config as "update").
# Runtime state (config.json, uploads/, .boxid, wifi) is gitignored / outside the
# repo, so checkout never touches it.
# ponytail: no lock file — systemd serializes starts of a oneshot unit.
set -u

# Everything lives in main(): `git checkout` rewrites this very file while bash
# is still reading it, so the whole script must be parsed before anything runs.
main() {
  APP="$(cd "$(dirname "$0")/.." && pwd)"
  APP_USER="$(stat -c %U "$APP")"
  STATE="$APP/update-state.json"
  HEALTH_URL="http://localhost:8770/health"
  AS_USER=(sudo -H -u "$APP_USER" env GIT_TERMINAL_PROMPT=0)
  git_() { "${AS_USER[@]}" git -C "$APP" "$@"; }

  STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  FROM="$(tr -d '[:space:]' < "$APP/VERSION" 2>/dev/null)"
  TO=""

  # write_state STATUS [ERROR]  — JSON built by python so quotes in errors are safe
  write_state() {
    echo "update: $1${2:+ — $2}"
    python3 - "$STATE" "$1" "$FROM" "$TO" "$STARTED" "${2:-}" <<'PY'
import datetime, json, os, sys
path, status, frm, to, started, err = sys.argv[1:7]
st = {"status": status, "from": frm, "to": to, "startedAt": started}
if status != "running":
    st["finishedAt"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
if err:
    st["error"] = err
with open(path + ".tmp", "w", encoding="utf-8") as f:
    json.dump(st, f, ensure_ascii=False, indent=2)
os.replace(path + ".tmp", path)
PY
    chown "$APP_USER" "$STATE" 2>/dev/null
  }

  # wait_healthy VERSION — /health must answer 200 with that version within 60 s
  wait_healthy() {
    local end=$((SECONDS + 60)) out code v
    HEALTH_ERR="geen antwoord"
    while [ "$SECONDS" -lt "$end" ]; do
      out="$(curl -s -m 3 -w '\n%{http_code}' "$HEALTH_URL")"
      code="${out##*$'\n'}"
      if [ "$code" = "200" ]; then
        v="$(printf '%s' "${out%$'\n'*}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("version",""))' 2>/dev/null)"
        [ "$v" = "$1" ] && return 0
        HEALTH_ERR="server meldt versie '$v', verwacht '$1'"
      else
        HEALTH_ERR="/health gaf HTTP ${code:-000}"
      fi
      sleep 1
    done
    return 1
  }

  write_state running

  if ! out="$(timeout 120 "${AS_USER[@]}" git -C "$APP" fetch --tags origin 2>&1)"; then
    write_state failed "ophalen van releases mislukt: $(printf '%s' "$out" | tail -n 2 | tr '\n' ' ')"
    return 0
  fi

  # newest vX.Y.Z tag; pre-release/test tags (with a '-') are never picked
  TAG="$(git_ tag -l 'v[0-9]*' --sort=-version:refname | grep -v -- - | head -1)"
  if [ -z "$TAG" ]; then
    write_state failed "geen release-tags (vX.Y.Z) gevonden op origin"
    return 0
  fi
  TO="$(git_ show "$TAG:VERSION" 2>/dev/null | tr -d '[:space:]')"
  TO="${TO:-dev}"

  # already on (or ahead of) the newest release → never downgrade
  if git_ merge-base --is-ancestor "$TAG" HEAD 2>/dev/null; then
    write_state up-to-date
    return 0
  fi

  PREV_REF="$(git_ rev-parse HEAD)"
  if ! out="$(git_ checkout --force "$TAG" 2>&1)"; then
    git_ checkout --force "$PREV_REF" >/dev/null 2>&1
    write_state failed "checkout van $TAG mislukt: $(printf '%s' "$out" | tail -n 2 | tr '\n' ' ')"
    return 0
  fi

  systemctl restart toernooitv-server
  if wait_healthy "$TO"; then
    systemctl restart toernooitv-kiosk 2>/dev/null
    write_state ok
    return 0
  fi

  ERR="$HEALTH_ERR"
  git_ checkout --force "$PREV_REF" >/dev/null 2>&1
  systemctl restart toernooitv-server
  if wait_healthy "${FROM:-dev}"; then
    write_state rolled-back "versie $TO startte niet goed ($ERR)"
  else
    write_state rolled-back "versie $TO startte niet goed ($ERR); ook de vorige versie antwoordt nog niet ($HEALTH_ERR)"
  fi
  # the kiosk may have reloaded onto an error page while the server was down
  systemctl restart toernooitv-kiosk 2>/dev/null
  return 0
}
main "$@"; exit $?
