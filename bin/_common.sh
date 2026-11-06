# Shared helpers; sourced by the other bin/ scripts.
# Nothing here runs as root: the scripts call sudo themselves where needed.
# Under `sudo bin/install-launchd` the jobs would be installed to run as
# root, which cannot read the data volume and leaves root-owned files.
if [ "$(id -u)" = 0 ]; then
  echo "$(basename "$0"): run this as yourself, not with sudo (it asks for sudo when it needs it)" >&2
  exit 1
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UV=/opt/homebrew/bin/uv
[ -x "$UV" ] || UV="$(command -v uv)"
LOG_DIR="$ROOT/var/log"
DAEMONS_DIR=/Library/LaunchDaemons
SYS=system
WEB_LABEL=com.sefk.westmarch.web
HOURLY_LABEL=com.sefk.westmarch.load-hourly
DAILY_LABEL=com.sefk.westmarch.load-daily
LABELS=("$WEB_LABEL" "$HOURLY_LABEL" "$DAILY_LABEL")
# Per-user LaunchAgent for loaders that need the login Keychain (cadence
# "session"). Lives in gui/<uid>, so it only runs while you are logged in.
AGENTS_DIR="$HOME/Library/LaunchAgents"
GUI="gui/$(id -u)"
SESSION_LABEL=com.sefk.westmarch.load-session
AGENT_LABELS=("$SESSION_LABEL")
unset VIRTUAL_ENV

load_env() {
  set -a; [ -f "$ROOT/.env" ] && . "$ROOT/.env"; set +a
  PORT="${WESTMARCH_PORT:-8520}"
}

ensure_env() {
  [ -f "$ROOT/.env" ] || cp "$ROOT/.env.example" "$ROOT/.env"
  mkdir -p "$LOG_DIR"
}
