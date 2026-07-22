#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${REPO_ROOT}/deploy/docker/compose.yml}"
ENV_FILE="${ENV_FILE:-${REPO_ROOT}/deploy/docker/.env.runtime}"
DEPLOY_DIR="$(cd -- "$(dirname -- "${COMPOSE_FILE}")" && pwd)"
STATE_DIR="${DEPLOY_DIR}/state"
DATA_DIR="${STATE_DIR}/data"
TELETHON_DIR="${STATE_DIR}/telethon"
JESSICA_SERVICE="${1:-${JESSICA_SERVICE:-tg-bot.service}}"
NATALIE_SERVICE="${2:-${NATALIE_SERVICE:-tg-bot-natalie.service}}"

declare -A ENV_MAP=()
ROLLBACK_NEEDED=0

log() {
  printf '[migrate] %s\n' "$*"
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command not found: $1" >&2
    exit 1
  fi
}

resolve_path() {
  python3 - "$1" "$2" <<'PY'
import os
import sys

path = sys.argv[1]
base = sys.argv[2]
if os.path.isabs(path):
    print(os.path.abspath(path))
else:
    print(os.path.abspath(os.path.join(base, path)))
PY
}

service_pid() {
  local service="$1"
  systemctl show -p MainPID --value "$service"
}

service_workdir() {
  local service="$1"
  systemctl show -p WorkingDirectory --value "$service"
}

collect_env() {
  local service="$1"
  local pid="$2"
  local line key value

  while IFS= read -r line; do
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      BOT_TOKEN|ADMIN_IDS|GIRLLM_DB|LM_TIMEOUT|LOG_LEVEL|LM_BASE|LM_MODEL|JESSICA_OPENROUTER_API_KEY|OPENROUTER_API_KEY|OPENROUTER_HTTP_REFERER|OPENROUTER_TITLE|MAX_CONCURRENT|NATALIE_LM_TIMEOUT|NATALIE_LM_BASE|NATALIE_LM_MODEL|NATALIE_OPENROUTER_API_KEY|NATALIE_API_ID|NATALIE_API_HASH|NATALIE_SESSION|NATALIE_PHONE)
        if [[ -n "${ENV_MAP[$key]+x}" && "${ENV_MAP[$key]}" != "$value" ]]; then
          echo "Conflicting value for ${key} between services. Refusing to guess." >&2
          exit 1
        fi
        ENV_MAP["$key"]="$value"
        ;;
    esac
  done < <(tr '\0' '\n' < "/proc/${pid}/environ")

  log "Captured environment from ${service} (pid ${pid})"
}

copy_file_family() {
  local src="$1"
  local dest_dir="$2"
  local base

  [[ -f "$src" ]] || return 0

  base="$(basename -- "$src")"
  cp -f "$src" "${dest_dir}/${base}"

  for suffix in -wal -shm -journal; do
    if [[ -f "${src}${suffix}" ]]; then
      cp -f "${src}${suffix}" "${dest_dir}/${base}${suffix}"
    fi
  done
}

write_runtime_env() {
  local db_basename="$1"
  local session_value="$2"

  mkdir -p "$DEPLOY_DIR"
  cat > "$ENV_FILE" <<EOF
BOT_TOKEN=${ENV_MAP[BOT_TOKEN]:-}
ADMIN_IDS=${ENV_MAP[ADMIN_IDS]:-}
LOG_LEVEL=${ENV_MAP[LOG_LEVEL]:-INFO}
MAX_CONCURRENT=${ENV_MAP[MAX_CONCURRENT]:-1}

GIRLLM_DB_BASENAME=${db_basename}

LM_TIMEOUT=${ENV_MAP[LM_TIMEOUT]:-300}
LM_BASE=${ENV_MAP[LM_BASE]:-}
LM_MODEL=${ENV_MAP[LM_MODEL]:-}
JESSICA_OPENROUTER_API_KEY=${ENV_MAP[JESSICA_OPENROUTER_API_KEY]:-}

NATALIE_LM_TIMEOUT=${ENV_MAP[NATALIE_LM_TIMEOUT]:-${ENV_MAP[LM_TIMEOUT]:-300}}
NATALIE_LM_BASE=${ENV_MAP[NATALIE_LM_BASE]:-}
NATALIE_LM_MODEL=${ENV_MAP[NATALIE_LM_MODEL]:-}
NATALIE_OPENROUTER_API_KEY=${ENV_MAP[NATALIE_OPENROUTER_API_KEY]:-}

OPENROUTER_API_KEY=${ENV_MAP[OPENROUTER_API_KEY]:-}
OPENROUTER_HTTP_REFERER=${ENV_MAP[OPENROUTER_HTTP_REFERER]:-}
OPENROUTER_TITLE=${ENV_MAP[OPENROUTER_TITLE]:-}

NATALIE_API_ID=${ENV_MAP[NATALIE_API_ID]:-}
NATALIE_API_HASH=${ENV_MAP[NATALIE_API_HASH]:-}
NATALIE_SESSION=${session_value}
NATALIE_PHONE=${ENV_MAP[NATALIE_PHONE]:-}

UPDATE_PULL_COMMAND=
UPDATE_RESTART_COMMAND=
EOF
}

rollback() {
  if [[ "$ROLLBACK_NEEDED" -eq 1 ]]; then
    log "Migration failed; restoring systemd services"
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" down >/dev/null 2>&1 || true
    systemctl start "$JESSICA_SERVICE" "$NATALIE_SERVICE" >/dev/null 2>&1 || true
  fi
}

trap rollback ERR

require_cmd systemctl
require_cmd docker
require_cmd python3

if ! docker compose version >/dev/null 2>&1; then
  echo "docker compose is not available on this host." >&2
  exit 1
fi

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "Compose file not found: $COMPOSE_FILE" >&2
  exit 1
fi

JESSICA_PID="$(service_pid "$JESSICA_SERVICE")"
NATALIE_PID="$(service_pid "$NATALIE_SERVICE")"

if [[ "${JESSICA_PID}" == "0" || "${NATALIE_PID}" == "0" ]]; then
  echo "Both systemd services must be running so their live environment can be captured." >&2
  exit 1
fi

JESSICA_WORKDIR="$(service_workdir "$JESSICA_SERVICE")"
NATALIE_WORKDIR="$(service_workdir "$NATALIE_SERVICE")"
JESSICA_WORKDIR="${JESSICA_WORKDIR:-$REPO_ROOT}"
NATALIE_WORKDIR="${NATALIE_WORKDIR:-$JESSICA_WORKDIR}"

collect_env "$JESSICA_SERVICE" "$JESSICA_PID"
collect_env "$NATALIE_SERVICE" "$NATALIE_PID"

DB_SOURCE_RAW="${ENV_MAP[GIRLLM_DB]:-data/girllm.sqlite}"
DB_SOURCE="$(resolve_path "$DB_SOURCE_RAW" "$NATALIE_WORKDIR")"
DB_BASENAME="$(basename -- "$DB_SOURCE")"

SESSION_SOURCE_RAW="${ENV_MAP[NATALIE_SESSION]:-}"
SESSION_TARGET_VALUE="$SESSION_SOURCE_RAW"

if [[ "$SESSION_SOURCE_RAW" == *.session ]]; then
  SESSION_SOURCE="$(resolve_path "$SESSION_SOURCE_RAW" "$NATALIE_WORKDIR")"
  SESSION_TARGET_VALUE="/state/telethon/$(basename -- "$SESSION_SOURCE")"
fi

mkdir -p "$DATA_DIR" "$TELETHON_DIR"
write_runtime_env "$DB_BASENAME" "$SESSION_TARGET_VALUE"

log "Building Docker image before the cutover"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" build

ROLLBACK_NEEDED=1

log "Stopping systemd services"
systemctl stop "$JESSICA_SERVICE" "$NATALIE_SERVICE"

if [[ -f "$DB_SOURCE" ]]; then
  log "Copying SQLite state from $DB_SOURCE"
  copy_file_family "$DB_SOURCE" "$DATA_DIR"
else
  log "Database file not found at $DB_SOURCE; creating empty state directory"
fi

if [[ "$SESSION_SOURCE_RAW" == *.session && -n "${SESSION_SOURCE:-}" ]]; then
  if [[ -f "$SESSION_SOURCE" ]]; then
    log "Copying Telethon session from $SESSION_SOURCE"
    copy_file_family "$SESSION_SOURCE" "$TELETHON_DIR"
  else
    echo "Telethon session file not found: $SESSION_SOURCE" >&2
    exit 1
  fi
fi

log "Fixing state directory ownership for container user (uid 10001)"
chown -R 10001:10001 "$STATE_DIR"

log "Starting Docker services"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d

for _ in $(seq 1 15); do
  RUNNING_SERVICES="$(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" ps --status running --services || true)"
  if grep -qx "jessica" <<<"$RUNNING_SERVICES" && grep -qx "natalie" <<<"$RUNNING_SERVICES"; then
    break
  fi
  sleep 2
done

RUNNING_SERVICES="$(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" ps --status running --services || true)"
if ! grep -qx "jessica" <<<"$RUNNING_SERVICES" || ! grep -qx "natalie" <<<"$RUNNING_SERVICES"; then
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" logs --no-color --tail 100 >&2 || true
  echo "Docker services did not both reach running state." >&2
  exit 1
fi

log "Docker is running; disabling old systemd units"
systemctl disable "$JESSICA_SERVICE" "$NATALIE_SERVICE" >/dev/null

ROLLBACK_NEEDED=0

cat <<EOF

Migration complete.

Runtime env:     $ENV_FILE
Compose file:    $COMPOSE_FILE
State directory: $STATE_DIR

Common follow-up commands:
  ${REPO_ROOT}/scripts/docker_stack.sh ps
  ${REPO_ROOT}/scripts/docker_stack.sh logs -f
  ${REPO_ROOT}/scripts/docker_stack.sh up -d --build
EOF

