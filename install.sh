#!/usr/bin/env bash
#
# One-line bootstrap for a Toernooi TV box on a fresh Raspberry Pi OS:
#   - installs git
#   - clones (or fetches) the repo as the normal user into ~/toernooi-tv
#   - checks out the newest release tag (vX.Y.Z; stays on main when there is none)
#   - runs install-kiosk.sh
#
# Run from a normal user's shell:
#   curl -fsSL https://raw.githubusercontent.com/woutr-nl/toernooiTV/main/install.sh | sudo bash
# Overrides (env): TOERNOOITV_USER, TOERNOOITV_DIR, TOERNOOITV_REBOOT=1.
# Idempotent — safe to re-run.

set -euo pipefail

# Everything lives in main(): with `curl | sudo bash` the script is read from
# stdin, so it must be parsed completely before apt-get/git/install-kiosk.sh run
# and could swallow the rest of it from the pipe.
main() {
  REPO_URL="https://github.com/woutr-nl/toernooiTV.git"

  die() { echo "$*" >&2; exit 1; }

  # newest vX.Y.Z tag; pre-release/test tags (with a '-') are never picked
  # args: git command prefix (e.g. git -C DIR)
  newest_release_tag() {
    "$@" tag -l 'v[0-9]*' --sort=-version:refname | grep -v -- - | head -1 || true
  }

  # test hook: print the tag that would be checked out, unprivileged
  if [ "${1:-}" = "--print-tag" ]; then
    newest_release_tag git -C "$2"
    exit 0
  fi

  if [ "$(id -u)" -ne 0 ]; then
    die "Please run with sudo:  curl -fsSL https://raw.githubusercontent.com/woutr-nl/toernooiTV/main/install.sh | sudo bash"
  fi

  TV_USER="${TOERNOOITV_USER:-${SUDO_USER:-}}"
  if [ -z "$TV_USER" ] || [ "$TV_USER" = "root" ]; then
    die "Can't tell which normal user will run the box: run this with sudo from that user's shell, or set TOERNOOITV_USER=<name>."
  fi
  id -u "$TV_USER" >/dev/null 2>&1 || die "User '$TV_USER' does not exist."

  TV_HOME="$(getent passwd "$TV_USER" | cut -d: -f6)"
  TV_DIR="${TOERNOOITV_DIR:-$TV_HOME/toernooi-tv}"
  case "$TV_DIR" in
    *" "*) die "The checkout path contains a space ($TV_DIR) — set TOERNOOITV_DIR to a path without spaces.";;
  esac
  AS_USER=(sudo -H -u "$TV_USER" env GIT_TERMINAL_PROMPT=0)
  echo "==> Installing Toernooi TV for user $TV_USER into $TV_DIR"

  echo "==> Installing git…"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y git

  if [ -e "$TV_DIR" ]; then
    # read origin as root straight from the config: no ownership check, so a
    # root-owned earlier clone is still recognised (and chowned below)
    url="$(git config --file "$TV_DIR/.git/config" --get remote.origin.url 2>/dev/null || true)"
    case "$url" in
      *toernooiTV*) ;;
      *) die "$TV_DIR exists but is not a checkout of toernooiTV — remove it, or set TOERNOOITV_DIR to another path.";;
    esac
    chown -R "$TV_USER": "$TV_DIR"
    echo "==> Existing checkout found — fetching updates…"
    "${AS_USER[@]}" git -C "$TV_DIR" fetch --tags origin
  else
    echo "==> Cloning $REPO_URL…"
    "${AS_USER[@]}" git clone "$REPO_URL" "$TV_DIR"
  fi

  TAG="$(newest_release_tag "${AS_USER[@]}" git -C "$TV_DIR")"
  if [ -n "$TAG" ]; then
    echo "==> Checking out release $TAG…"
    "${AS_USER[@]}" git -C "$TV_DIR" checkout --force "$TAG"
  else
    echo "==> No release tags (vX.Y.Z) yet — staying on main"
    "${AS_USER[@]}" git -C "$TV_DIR" checkout --force -B main origin/main
  fi

  bash "$TV_DIR/install-kiosk.sh"

  if [ "${TOERNOOITV_REBOOT:-}" = "1" ]; then
    echo "==> Rebooting…"
    reboot
    return 0
  fi
  echo
  echo "==> Done. Next steps:"
  echo "    1. sudo reboot                      — starts the kiosk on the TV"
  echo "    2. after the reboot:  cd $TV_DIR && bash ./verify-appliance.sh"
  echo "    3. manage it from another device:  http://toernooitv.local/beheer"
  echo "    4. the portal link code appears on the TV screen"
}
main "$@"; exit $?
