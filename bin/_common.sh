# Shared helpers; sourced by the other bin/ scripts.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UV=/opt/homebrew/bin/uv
[ -x "$UV" ] || UV="$(command -v uv)"
LOG_DIR="$ROOT/var/log"
DAEMONS_DIR=/Library/LaunchDaemons
SYS=system
WEB_LABEL=com.sefk.life-metrics.web
HOURLY_LABEL=com.sefk.life-metrics.load-hourly
DAILY_LABEL=com.sefk.life-metrics.load-daily
LABELS=("$WEB_LABEL" "$HOURLY_LABEL" "$DAILY_LABEL")
unset VIRTUAL_ENV

load_env() {
  set -a; [ -f "$ROOT/.env" ] && . "$ROOT/.env"; set +a
  PORT="${LIFE_PORT:-8520}"
}

ensure_env() {
  [ -f "$ROOT/.env" ] || cp "$ROOT/.env.example" "$ROOT/.env"
  mkdir -p "$LOG_DIR"
}
