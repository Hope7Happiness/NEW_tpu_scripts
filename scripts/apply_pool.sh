ZHH_APPLY_ROOT="${ZHH_CENTER_ROOT:-/kmh-nfs-ssd-us-mount/staging/.tpu_center}/apply_sessions"
ZHH_APPLY_LOG_ROOT="${ZHH_CENTER_ROOT:-/kmh-nfs-ssd-us-mount/staging/.tpu_center}/apply_logs"
ZHH_APPLY_SESSION_PREFIX="zhh_apply_"

zapply_usage(){
    echo "Usage: wxb apply <v5p-32|v5p-64|v5p-128|v6e-32|v6e-64|v6e-128> <zone> <session_number>" >&2
}

zapply_del_usage(){
    echo "Usage: wxb apply-del <tpu_type> <zone> <session_number>" >&2
}

zapply_validate_tpu_type(){
    case "$1" in
        v5p-32|v5p-64|v5p-128|v6e-32|v6e-64|v6e-128)
            return 0
            ;;
        *)
            echo "Unsupported tpu_type: $1" >&2
            zapply_usage
            return 1
            ;;
    esac
}

zapply_validate_count(){
    local count="$1"
    if [[ ! "$count" =~ ^[0-9]+$ ]] || [ "$count" -le 0 ]; then
        echo "session_number must be a positive integer: $count" >&2
        return 1
    fi
}

zapply_safe_name(){
    zhh_sanitize_name "$1" | tr '-' '_'
}

zapply_random_hex(){
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 3
    else
        tr -dc 'a-f0-9' < /dev/urandom | head -c 6
    fi
}

zapply_ensure_layout(){
    mkdir -p "$ZHH_APPLY_ROOT" "$ZHH_APPLY_LOG_ROOT"
    chmod 777 "$ZHH_APPLY_ROOT" "$ZHH_APPLY_LOG_ROOT" 2>/dev/null || true
}

zapply_state_path(){
    printf '%s/%s.json' "$ZHH_APPLY_ROOT" "$1"
}

zapply_log_path(){
    printf '%s/%s.log' "$ZHH_APPLY_LOG_ROOT" "$1"
}

zapply_write_state(){
    local session="$1"
    local status="$2"
    local tpu_type="$3"
    local zone="$4"
    local vm_name="${5:-}"
    local last_error="${6:-}"
    local state_path=""
    local log_path=""

    zapply_ensure_layout
    state_path=$(zapply_state_path "$session")
    log_path=$(zapply_log_path "$session")

    ZAPPLY_STATE_PATH="$state_path" \
    ZAPPLY_SESSION="$session" \
    ZAPPLY_STATUS="$status" \
    ZAPPLY_TPU_TYPE="$tpu_type" \
    ZAPPLY_ZONE="$zone" \
    ZAPPLY_VM_NAME="$vm_name" \
    ZAPPLY_LAST_ERROR="$last_error" \
    ZAPPLY_LOG_PATH="$log_path" \
    python3 - <<'PY'
import json
import os
import time
from pathlib import Path

path = Path(os.environ["ZAPPLY_STATE_PATH"])
old = {}
if path.exists():
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        old = {}

def normalize_error(text):
    text = str(text or "")
    lowered = text.lower()
    if "request a higher quota limit" in lowered or "request_increase" in lowered:
        return "request quota limit"
    return " ".join(text.split())

payload = {
    "session": os.environ["ZAPPLY_SESSION"],
    "status": os.environ["ZAPPLY_STATUS"],
    "tpu_type": os.environ["ZAPPLY_TPU_TYPE"],
    "zone": os.environ["ZAPPLY_ZONE"],
    "current_vm": os.environ["ZAPPLY_VM_NAME"],
    "pid": os.getppid(),
    "started_at": old.get("started_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "last_success_at": old.get("last_success_at"),
    "success_count": int(old.get("success_count") or 0),
    "attempt_count": int(old.get("attempt_count") or 0),
    "last_error": normalize_error(os.environ["ZAPPLY_LAST_ERROR"]),
    "log": os.environ["ZAPPLY_LOG_PATH"],
}
if payload["status"] in ("CREATED", "KEEPALIVE", "SLEEPING"):
    payload["last_success_at"] = payload["updated_at"]
    if old.get("current_vm") != payload["current_vm"]:
        payload["success_count"] += 1
if payload["status"] in ("APPLYING", "CREATE_FAILED", "WAIT_READY", "SETUP", "KEEPALIVE"):
    payload["attempt_count"] += 1
tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.chmod(tmp, 0o666)
tmp.replace(path)
PY
}

zapply_retry_sleep(){
    local message="$1"
    local sleep_seconds="${ZHH_APPLY_RETRY_SLEEP_SECONDS:-30}"
    if [[ "$message" =~ [Qq]uota|[Ll]imit|RESOURCE_EXHAUSTED ]]; then
        sleep_seconds="${ZHH_APPLY_QUOTA_SLEEP_SECONDS:-300}"
    fi
    sleep $((sleep_seconds + RANDOM % 30))
}

zapply_wait_ready(){
    local vm_name="$1"
    local zone="$2"
    local round=0
    local status=""
    local max_rounds="${ZHH_APPLY_READY_ROUNDS:-90}"

    while [ "$round" -lt "$max_rounds" ]; do
        status=$("$CUSTOM_GCLOUD_EXE" compute tpus tpu-vm describe "$vm_name" --zone="$zone" --format="value(state)" 2>/dev/null || true)
        if [ "$status" = "READY" ]; then
            return 0
        fi
        if [ "$status" = "PREEMPTED" ] || [ "$status" = "DELETED" ]; then
            return 1
        fi
        round=$((round + 1))
        sleep 10
    done
    return 1
}

zapply_create_legacy_lock(){
    local vm_name="$1"
    local lock_file="/kmh-nfs-ssd-us-mount/code/qiao/tpu_lock/xianbang_${vm_name}_$(date -u +%Y-%m-%d_%H-%M-%S)"
    sudo mkdir -p /kmh-nfs-ssd-us-mount/code/qiao/tpu_lock 2>/dev/null || true
    sudo touch "$lock_file" 2>/dev/null || true
    sudo chmod 666 "$lock_file" 2>/dev/null || true
}

zapply_start_matmul(){
    local vm_name="$1"
    local zone="$2"
    local py_path="$CONDA_PY_PATH"
    local dbg_commands="ls $CONDA_PY_PATH"
    local matmul_script=""
    local command=""

    if use_v6_script "$vm_name"; then
        py_path="python"
        dbg_commands="which python"
    fi

    matmul_script="import jax as j,time as t;from flax.jax_utils import replicate as e;p=j.numpy;r=j.random;k=r.PRNGKey(0);N=3<<14;_T=e(r.normal(k,(N,N)));__=j.pmap(lambda _: _.T@_/p.linalg.norm(_@_.T));exec('while True: (__(_T), t.sleep(0.5))')"
    command="nohup $py_path -c \"$matmul_script\" >/tmp/zhh_apply_matmul.log 2>&1 < /dev/null &"
    "$CUSTOM_GCLOUD_EXE" compute tpus tpu-vm ssh "$vm_name" --zone "$zone" --worker=all --command "$dbg_commands && $command"
}

zapply_worker(){
    local tpu_type="$1"
    local zone="$2"
    local session="$3"
    local sleep_seconds="${ZHH_APPLY_SUCCESS_SLEEP_SECONDS:-1800}"
    local vm_name=""
    local output=""
    local ret=0
    local accelerator_version=""
    local service_account=""
    local log_path=""
    local old_script_debug=""
    local had_script_debug=false

    set +e
    zapply_validate_tpu_type "$tpu_type" || return 1
    if [ -z "$zone" ] || [ -z "$session" ]; then
        zapply_usage
        return 1
    fi
    zapply_ensure_layout
    log_path=$(zapply_log_path "$session")
    touch "$log_path"
    chmod 666 "$log_path" 2>/dev/null || true

    trap 'zapply_write_state "$session" "STOPPED" "$tpu_type" "$zone" "$vm_name" "stopped"; exit 0' INT TERM HUP EXIT

    while true; do
        vm_name="kmh-tpuvm-${tpu_type}-xianbang-$(zapply_random_hex)"
        export VM_NAME="$vm_name"
        export ZONE="$zone"
        accelerator_version=$(get_accelerator_version "$vm_name")
        ret=$?
        if [ $ret -ne 0 ] || [ -z "$accelerator_version" ]; then
            zapply_write_state "$session" "CONFIG_ERROR" "$tpu_type" "$zone" "$vm_name" "failed to resolve accelerator version"
            sleep 300
            continue
        fi
        service_account=$(get_service_account)
        ret=$?
        if [ $ret -ne 0 ] || [ -z "$service_account" ]; then
            zapply_write_state "$session" "CONFIG_ERROR" "$tpu_type" "$zone" "$vm_name" "failed to resolve service account"
            sleep 300
            continue
        fi

        zapply_write_state "$session" "APPLYING" "$tpu_type" "$zone" "$vm_name" ""
        {
            printf '\n[%s] create %s @ %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$vm_name" "$zone"
            "$CUSTOM_GCLOUD_EXE" compute tpus tpu-vm create "$vm_name" \
                --zone="$zone" \
                --accelerator-type="$tpu_type" \
                --version="$accelerator_version" \
                --spot \
                --service-account="$service_account"
        } >> "$log_path" 2>&1
        ret=$?
        if [ $ret -ne 0 ]; then
            output=$(tail -n 20 "$log_path" 2>/dev/null | tr '\n' ' ')
            zapply_write_state "$session" "CREATE_FAILED" "$tpu_type" "$zone" "$vm_name" "$output"
            zapply_retry_sleep "$output"
            continue
        fi

        zapply_write_state "$session" "WAIT_READY" "$tpu_type" "$zone" "$vm_name" ""
        if ! zapply_wait_ready "$vm_name" "$zone" >> "$log_path" 2>&1; then
            zapply_write_state "$session" "READY_FAILED" "$tpu_type" "$zone" "$vm_name" "TPU did not become READY"
            zapply_retry_sleep "not ready"
            continue
        fi

        zapply_create_legacy_lock "$vm_name"
        export TPU_IS_NEW=1
        export ZHH_SETUP_TRIAL=1
        if [ "${SCRIPT_DEBUG+x}" = "x" ]; then
            had_script_debug=true
            old_script_debug="$SCRIPT_DEBUG"
        else
            had_script_debug=false
        fi
        export SCRIPT_DEBUG=1
        zapply_write_state "$session" "SETUP" "$tpu_type" "$zone" "$vm_name" ""
        run_setup_script "$vm_name" "$zone" >> "$log_path" 2>&1
        ret=$?
        if $had_script_debug; then
            export SCRIPT_DEBUG="$old_script_debug"
        else
            unset SCRIPT_DEBUG
        fi
        unset TPU_IS_NEW
        unset ZHH_SETUP_TRIAL
        if [ $ret -ne 0 ]; then
            zapply_write_state "$session" "SETUP_FAILED" "$tpu_type" "$zone" "$vm_name" "setup exited $ret"
        fi

        zapply_write_state "$session" "KEEPALIVE" "$tpu_type" "$zone" "$vm_name" ""
        zapply_start_matmul "$vm_name" "$zone" >> "$log_path" 2>&1
        ret=$?
        if [ $ret -ne 0 ]; then
            zapply_write_state "$session" "KEEPALIVE_FAILED" "$tpu_type" "$zone" "$vm_name" "matmul exited $ret"
        else
            register_tpu >> "$log_path" 2>&1 || true
            zapply_write_state "$session" "SLEEPING" "$tpu_type" "$zone" "$vm_name" ""
        fi
        sleep "$sleep_seconds"
    done
}

zapply_start(){
    local tpu_type="$1"
    local zone="$2"
    local count="$3"
    local i=0
    local session=""
    local safe_type=""
    local safe_zone=""
    local command=""

    zapply_validate_tpu_type "$tpu_type" || return 1
    if [ -z "$zone" ]; then
        zapply_usage
        return 1
    fi
    zapply_validate_count "$count" || return 1
    if ! command -v tmux >/dev/null 2>&1; then
        echo "tmux not found" >&2
        return 1
    fi

    zapply_ensure_layout
    safe_type=$(zapply_safe_name "$tpu_type")
    safe_zone=$(zapply_safe_name "$zone")
    for i in $(seq 1 "$count"); do
        session="${ZHH_APPLY_SESSION_PREFIX}${safe_type}_${safe_zone}_$(date -u +%Y%m%d_%H%M%S)_$(zapply_random_hex)"
        zapply_write_state "$session" "STARTING" "$tpu_type" "$zone" "" ""
        command="exec \"$ZHH_SCRIPT_ROOT/main.sh\" apply-worker \"$tpu_type\" \"$zone\" \"$session\""
        tmux new-session -d -s "$session" "$command"
        printf 'started %s (%s @ %s)\n' "$session" "$tpu_type" "$zone"
    done
}

zapply_what(){
    zapply_ensure_layout
    ZAPPLY_ROOT="$ZHH_APPLY_ROOT" ZAPPLY_PREFIX="$ZHH_APPLY_SESSION_PREFIX" python3 - <<'PY'
import json
import os
import subprocess
import time
from pathlib import Path

root = Path(os.environ["ZAPPLY_ROOT"])
prefix = os.environ["ZAPPLY_PREFIX"]
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[90m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BLUE = "\033[34m"
CYAN = "\033[36m"

def ctext(color, text):
    return f"{color}{text}{RESET}"

def print_kv(label, value):
    print(f"\t{DIM}{label + ':':<14}{RESET} {value}")

def normalize_error(text):
    text = str(text or "")
    lowered = text.lower()
    if "request a higher quota limit" in lowered or "request_increase" in lowered:
        return "request quota limit"
    return " ".join(text.split())

def format_status(status):
    status = str(status or "-")
    if status in ("KEEPALIVE", "SLEEPING", "CREATED"):
        return ctext(GREEN + BOLD, status)
    if status in ("CREATE_FAILED", "READY_FAILED"):
        return ctext(YELLOW + BOLD, status)
    if status in ("CONFIG_ERROR", "SETUP_FAILED", "KEEPALIVE_FAILED", "STOPPED"):
        return ctext(RED + BOLD, status)
    if status in ("STARTING", "APPLYING", "WAIT_READY", "SETUP", "UNKNOWN"):
        return ctext(BLUE + BOLD, status)
    return status

def parse_epoch(value):
    if not value:
        return 0
    try:
        return int(time.mktime(time.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ")))
    except Exception:
        return 0

def fmt_age(value):
    epoch = parse_epoch(value)
    if not epoch:
        return "-"
    delta = max(0, int(time.time()) - epoch)
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"

def infer_from_session(session):
    if not session.startswith(prefix):
        return "-", "-"
    parts = session[len(prefix):].split("_")
    if len(parts) < 6:
        return "-", "-"
    tpu_type = f"{parts[0]}-{parts[1]}"
    zone = "-".join(parts[2:-3]) or "-"
    return tpu_type, zone

try:
    proc = subprocess.run(["tmux", "list-sessions", "-F", "#S"], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    live = set(proc.stdout.splitlines()) if proc.returncode == 0 else set()
except FileNotFoundError:
    live = set()

rows = []
seen = set()
for path in sorted(root.glob("*.json")):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        continue
    session = str(payload.get("session") or path.stem)
    seen.add(session)
    if session in live:
        rows.append({**payload, "alive": True})
for session in sorted(s for s in live if s.startswith(prefix) and s not in seen):
    tpu_type, zone = infer_from_session(session)
    rows.append({"session": session, "status": "UNKNOWN", "tpu_type": tpu_type, "zone": zone, "current_vm": "", "updated_at": "", "last_error": "", "alive": True})

if not rows:
    print("No wxb apply sessions found.")
    raise SystemExit(0)

rows.sort(key=lambda row: (str(row.get("tpu_type") or ""), str(row.get("zone") or ""), str(row.get("session") or "")))
print(f"TPU apply sessions root: {root}")
for row in rows:
    session = str(row.get("session") or "-")
    tpu_type = str(row.get("tpu_type") or "-")
    zone = str(row.get("zone") or "-")
    current_vm = str(row.get("current_vm") or "-")
    print()
    print(f"{ctext(BOLD, session)} · {ctext(BOLD, ctext(CYAN, tpu_type))} @ {ctext(BOLD, ctext(CYAN, zone))}")
    print_kv("status", format_status(row.get("status")))
    print_kv("current_vm", ctext(CYAN, current_vm))
    print_kv("attempts", row.get("attempt_count", 0))
    print_kv("successes", row.get("success_count", 0))
    print_kv("updated", f"{fmt_age(row.get('updated_at'))} ago")
    if row.get("last_success_at"):
        print_kv("last_success", f"{fmt_age(row.get('last_success_at'))} ago")
    if row.get("log"):
        print_kv("log", row.get("log"))
    err = normalize_error(row.get("last_error") or "")
    if err:
        print_kv("last_error", err[:220])
PY
}

zapply_del(){
    local tpu_type="$1"
    local zone="$2"
    local count="$3"
    local killed=0
    local removed_logs=0
    local session=""
    local log_path=""

    zapply_validate_tpu_type "$tpu_type" || return 1
    if [ -z "$zone" ]; then
        zapply_del_usage
        return 1
    fi
    zapply_validate_count "$count" || return 1
    zapply_ensure_layout

    while IFS= read -r session; do
        if [ -z "$session" ]; then
            continue
        fi
        tmux kill-session -t "$session" >/dev/null 2>&1 || true
        zapply_write_state "$session" "STOPPED" "$tpu_type" "$zone" "" "stopped by user"
        log_path=$(zapply_log_path "$session")
        if [ -f "$log_path" ]; then
            rm -f "$log_path" && removed_logs=$((removed_logs + 1))
        fi
        killed=$((killed + 1))
    done < <(
        ZAPPLY_ROOT="$ZHH_APPLY_ROOT" ZAPPLY_PREFIX="$ZHH_APPLY_SESSION_PREFIX" ZAPPLY_TPU_TYPE="$tpu_type" ZAPPLY_ZONE="$zone" ZAPPLY_COUNT="$count" python3 - <<'PY'
import json
import os
import subprocess
from pathlib import Path

root = Path(os.environ["ZAPPLY_ROOT"])
prefix = os.environ["ZAPPLY_PREFIX"]
tpu_type = os.environ["ZAPPLY_TPU_TYPE"]
zone = os.environ["ZAPPLY_ZONE"]
limit = int(os.environ["ZAPPLY_COUNT"])
proc = subprocess.run(["tmux", "list-sessions", "-F", "#S"], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
live = set(proc.stdout.splitlines()) if proc.returncode == 0 else set()
matches = []
for path in sorted(root.glob("*.json")):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        continue
    session = str(payload.get("session") or path.stem)
    if session in live and payload.get("tpu_type") == tpu_type and payload.get("zone") == zone:
        matches.append(session)
for session in sorted(s for s in live if s.startswith(prefix)):
    if session not in matches and tpu_type.replace("-", "_") in session and zone.replace("-", "_") in session:
        matches.append(session)
for session in matches[:limit]:
    print(session)
PY
    )

    printf 'stopped %s apply session(s) for %s @ %s\n' "$killed" "$tpu_type" "$zone"
    printf 'removed %s apply log(s)\n' "$removed_logs"
}
