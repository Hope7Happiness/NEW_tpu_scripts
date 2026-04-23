##### YOUR SETTINGS #####

CONDA_PY_PATH="/kmh-nfs-ssd-us-mount/code/hanhong/miniforge3/bin/python3" # your conda python path
# STAGING_NAME=<WECODE_USER> # your stage dir is at /kmh-nfs-ssd-us-mount/staging/<STAGING_NAME> # CURRENT DISABLED
GS_STAGING_NAME=qiao_zhicheng_hanhong_files # your gs staging dir is at gs://kmh-gcp-us-central2/<GS_STAGING_NAME>
TPU_DEFAULT_NAME=kangyang
SSCRIPT_HOME=/kmh-nfs-ssd-us-mount/staging/.sscript # the place that stores your tpu infos
WECODE_USER="${WECODE_USER:-${CURCHAT_USER:-${WHO:-}}}"
if [ -z "$WECODE_USER" ]; then
    WECODE_USER="$(whoami)"
fi
export WECODE_USER
export WHO="${WHO:-$WECODE_USER}"
CODE_HOME="/kmh-nfs-ssd-us-mount/code/siri"

##### END OF YOUR SETTINGS #####

# hint: ZHH_SCRIPT_ROOT will be defined in main.sh
semail(){
    python3 $ZHH_SCRIPT_ROOT/tools/pemail.py "$@" || zhh_warn "Failed to send email."
}

VM_UNFOUND_ERROR="\033[31m[Internal Error] VM_NAME is not set. Contact admin.\033[0m"
ZONE_UNFOUND_ERROR="\033[31m[Internal Error] ZONE is not set or incorrect. Contact admin.\033[0m"

CUSTOM_GCLOUD_EXE="/kmh-nfs-ssd-us-mount/code/siri/google-cloud-sdk/bin/gcloud"
ZHH_LOG_BAR_WIDTH=76
ZHH_COLOR_RESET=$'\033[0m'
ZHH_COLOR_BOLD=$'\033[1m'
ZHH_COLOR_BLUE=$'\033[34m'
ZHH_COLOR_GREEN=$'\033[32m'
ZHH_COLOR_YELLOW=$'\033[33m'
ZHH_COLOR_RED=$'\033[31m'
ZHH_COLOR_DIM=$'\033[90m'

zhh_repeat_char(){
    local char="$1"
    local count="${2:-$ZHH_LOG_BAR_WIDTH}"
    local out=""
    printf -v out '%*s' "$count" ''
    printf '%s\n' "${out// /$char}"
}

zhh_hr(){
    local char="${1:--}"
    zhh_repeat_char "$char" "$ZHH_LOG_BAR_WIDTH"
}

zhh_section(){
    local title="$1"
    printf '\n'
    printf '%b%s%b\n' "$ZHH_COLOR_BLUE$ZHH_COLOR_BOLD" "$title" "$ZHH_COLOR_RESET"
}

zhh_step_banner(){
    local title="$1"
    local log_file="$2"
    shift 2
    local details=()
    if [ -n "$log_file" ]; then
        details+=("$(zhh_format_detail "log file" "$log_file")")
    fi
    while [ $# -gt 0 ]; do
        details+=("$1")
        shift
    done
    zhh_step_begin "$title" "${details[@]}"
}

zhh_log_line(){
    local level="$1"
    shift
    printf '[%s] %s\n' "$level" "$*"
}

zhh_info(){
    printf '%bℹ️  %s%b\n' "$ZHH_COLOR_BLUE$ZHH_COLOR_BOLD" "$*" "$ZHH_COLOR_RESET"
}

zhh_muted_info(){
    printf '%bℹ️  %s%b\n' "$ZHH_COLOR_DIM" "$*" "$ZHH_COLOR_RESET"
}

zhh_warn(){
    printf '%b⚠️  %s%b\n' "$ZHH_COLOR_YELLOW$ZHH_COLOR_BOLD" "$*" "$ZHH_COLOR_RESET"
}

zhh_muted_warn(){
    printf '%b⚠️  %s%b\n' "$ZHH_COLOR_DIM" "$*" "$ZHH_COLOR_RESET"
}

zhh_error(){
    printf '%b❌ %s%b\n' "$ZHH_COLOR_RED$ZHH_COLOR_BOLD" "$*" "$ZHH_COLOR_RESET" >&2
}

zhh_success(){
    printf '%b✅ %s%b\n' "$ZHH_COLOR_GREEN$ZHH_COLOR_BOLD" "$*" "$ZHH_COLOR_RESET"
}

zhh_note(){
    printf '%b%s%b\n' "$ZHH_COLOR_DIM" "$*" "$ZHH_COLOR_RESET"
}

zhh_debug(){
    if [ -n "$SCRIPT_DEBUG" ]; then
        zhh_log_line DEBUG "$*"
    fi
}

zhh_kv(){
    local key="$1"
    shift
    printf '  %b%-14s%b %s\n' "$ZHH_COLOR_DIM" "$key:" "$ZHH_COLOR_RESET" "$*"
}

zhh_format_detail(){
    local key="$1"
    shift
    printf '%b%-14s%b %s' "$ZHH_COLOR_DIM" "$key:" "$ZHH_COLOR_RESET" "$*"
}

zhh_blue_text(){
    printf '%b%s%b' "$ZHH_COLOR_BLUE$ZHH_COLOR_BOLD" "$*" "$ZHH_COLOR_RESET"
}

zhh_green_text(){
    printf '%b%s%b' "$ZHH_COLOR_GREEN$ZHH_COLOR_BOLD" "$*" "$ZHH_COLOR_RESET"
}

zhh_red_text(){
    printf '%b%s%b' "$ZHH_COLOR_RED$ZHH_COLOR_BOLD" "$*" "$ZHH_COLOR_RESET"
}

zhh_yellow_text(){
    printf '%b%s%b' "$ZHH_COLOR_YELLOW$ZHH_COLOR_BOLD" "$*" "$ZHH_COLOR_RESET"
}

zhh_box_message(){
    local color="$1"
    local text="$2"
    local width=0
    local border=""
    width=$(( $(zhh_printable_length "$text") + 2 ))
    border=$(zhh_repeat_inline "─" "$width")
    printf '%b┌%s┐%b\n' "$color" "$border" "$ZHH_COLOR_RESET"
    printf '%b│ %s │%b\n' "$color" "$text" "$ZHH_COLOR_RESET"
    printf '%b└%s┘%b\n' "$color" "$border" "$ZHH_COLOR_RESET"
}

zhh_box_section(){
    printf '\n'
    zhh_box_message "$ZHH_COLOR_BLUE$ZHH_COLOR_BOLD" "$1"
}

zhh_box_success(){
    printf '\n'
    zhh_box_message "$ZHH_COLOR_GREEN$ZHH_COLOR_BOLD" "$1"
    printf '\n'
}

zhh_mask_secret(){
    local value="$1"
    local len=${#value}
    if [ -z "$value" ]; then
        printf '<empty>'
    elif [ $len -le 8 ]; then
        printf '********'
    else
        printf '%s...%s' "${value:0:4}" "${value: -4}"
    fi
}

zhh_sanitize_name(){
    local raw="$1"
    raw="${raw// /_}"
    raw="${raw//[^a-zA-Z0-9._-]/_}"
    printf '%s' "$raw"
}

zhh_strip_ansi(){
    printf '%s' "$1" | sed -E $'s/\x1B\[[0-9;]*[[:alpha:]]//g'
}

zhh_printable_length(){
    local clean=""
    clean=$(zhh_strip_ansi "$1")
    printf '%s' "${#clean}"
}

zhh_terminal_width(){
    local cols=""
    cols="${COLUMNS:-}"
    if [ -z "$cols" ]; then
        cols=$(tput cols 2>/dev/null || printf '120')
    fi
    if [ -z "$cols" ] || [ "$cols" -lt 40 ]; then
        cols=120
    fi
    printf '%s' "$cols"
}

zhh_visual_lines(){
    local text="$1"
    local width=""
    local length=0
    width=$(zhh_terminal_width)
    length=$(zhh_printable_length "$text")
    if [ "$length" -le 0 ]; then
        printf '1'
        return 0
    fi
    printf '%s' $(( (length - 1) / width + 1 ))
}

zhh_repeat_inline(){
    local char="$1"
    local count="$2"
    local out=""
    printf -v out '%*s' "$count" ''
    printf '%s' "${out// /$char}"
}

zhh_set_stage_context(){
    local stage_dir="$1"
    local stage_name=""
    if [ -z "$stage_dir" ]; then
        return 1
    fi
    export ZHH_STAGE_DIR="$stage_dir"
    stage_name=$(zhh_sanitize_name "$(basename "$stage_dir")")
    if [ -z "$ZHH_PREP_LOG_DIR" ] || [[ "$ZHH_PREP_LOG_DIR" != "$stage_dir"/logs/prep_* ]]; then
        export ZHH_PREP_LOG_DIR="$stage_dir/logs/prep_$(date +'%Y%m%d_%H%M%S')_${stage_name}_$$"
    fi
    mkdir -p "$ZHH_PREP_LOG_DIR"
}

zhh_prepare_log_file(){
    local __var_name="$1"
    local step_name="$2"
    local stage_dir="${ZHH_STAGE_DIR:-${STAGE_DIR:-}}"
    local safe_step=""
    local path=""
    if [ -z "$stage_dir" ]; then
        return 1
    fi
    zhh_set_stage_context "$stage_dir" || return 1
    safe_step=$(zhh_sanitize_name "$step_name")
    path="$ZHH_PREP_LOG_DIR/$(date +'%Y%m%d_%H%M%S')_${safe_step}_$RANDOM.log"
    : > "$path"
    printf -v "$__var_name" '%s' "$path"
}

zhh_prepare_ring_log_file(){
    local __var_name="$1"
    local step_name="$2"
    local slot_count="${3:-5}"
    local stage_dir="${ZHH_STAGE_DIR:-${STAGE_DIR:-}}"
    local safe_step=""
    local log_dir=""
    local counter_file=""
    local counter=0
    local slot=1
    local path=""
    if [ -z "$stage_dir" ]; then
        return 1
    fi
    safe_step=$(zhh_sanitize_name "$step_name")
    log_dir="$stage_dir/logs/${safe_step}_logs"
    mkdir -p "$log_dir"
    counter_file="$log_dir/.next_slot"
    if [ -f "$counter_file" ]; then
        counter=$(cat "$counter_file" 2>/dev/null || printf '0')
    fi
    if [[ ! "$counter" =~ ^[0-9]+$ ]]; then
        counter=0
    fi
    counter=$((counter + 1))
    slot=$(( ((counter - 1) % slot_count) + 1 ))
    printf '%s\n' "$counter" > "$counter_file"
    path="$log_dir/${safe_step}_${slot}.log"
    : > "$path"
    printf -v "$__var_name" '%s' "$path"
}

zhh_append_command_header(){
    local log_file="$1"
    shift
    if [ -z "$log_file" ]; then
        return 0
    fi
    printf '\n[%s] $ %s\n' "$(date +'%Y-%m-%d %H:%M:%S')" "$*" >> "$log_file"
}

zhh_run_logged_command(){
    local log_file="$1"
    shift
    if [ -n "$log_file" ]; then
        zhh_append_command_header "$log_file" "$*"
        "$@" >> "$log_file" 2>&1
        return $?
    fi
    "$@"
}

zhh_run_eval_logged(){
    local log_file="$1"
    local command="$2"
    if [ -n "$log_file" ]; then
        zhh_append_command_header "$log_file" "$command"
        eval "$command" >> "$log_file" 2>&1
        return $?
    fi
    eval "$command"
}

zhh_capture_command_output(){
    local __var_name="$1"
    local log_file="$2"
    shift 2
    local tmp_file=""
    local output=""
    local ret=0
    tmp_file=$(mktemp)
    zhh_append_command_header "$log_file" "$*"
    if "$@" > "$tmp_file" 2>&1; then
        ret=0
    else
        ret=$?
    fi
    if [ -f "$tmp_file" ]; then
        output=$(<"$tmp_file")
    fi
    if [ -n "$output" ] && [ -n "$log_file" ]; then
        printf '%s\n' "$output" >> "$log_file"
    fi
    rm -f "$tmp_file"
    printf -v "$__var_name" '%s' "$output"
    return $ret
}

zhh_capture_eval_output(){
    local __var_name="$1"
    local log_file="$2"
    local command="$3"
    local tmp_file=""
    local output=""
    local ret=0
    tmp_file=$(mktemp)
    zhh_append_command_header "$log_file" "$command"
    if eval "$command" > "$tmp_file" 2>&1; then
        ret=0
    else
        ret=$?
    fi
    if [ -f "$tmp_file" ]; then
        output=$(<"$tmp_file")
    fi
    if [ -n "$output" ] && [ -n "$log_file" ]; then
        printf '%s\n' "$output" >> "$log_file"
    fi
    rm -f "$tmp_file"
    printf -v "$__var_name" '%s' "$output"
    return $ret
}

zhh_progress_update(){
    printf '\r\033[2K%s' "$*"
}

zhh_progress_finish(){
    printf '\r\033[2K%s\n' "$*"
}

zhh_step_begin(){
    zhh_step_stop_spinner >/dev/null 2>&1 || true
    local title="$1"
    shift
    local move_lines=1
    local detail=""
    export ZHH_ACTIVE_STEP_TITLE="$title"
    printf '%b🔵 %s%b\n' "$ZHH_COLOR_BLUE$ZHH_COLOR_BOLD" "$title" "$ZHH_COLOR_RESET"
    while [ $# -gt 0 ]; do
        detail="$1"
        printf '  %s\n' "$detail"
        move_lines=$((move_lines + $(zhh_visual_lines "  $detail")))
        shift
    done
    export ZHH_ACTIVE_STEP_MOVE_LINES="$move_lines"
}

zhh_step_redraw(){
    local color="$1"
    local icon="$2"
    local suffix="$3"
    local title="${ZHH_ACTIVE_STEP_TITLE:-}"
    local move_lines="${ZHH_ACTIVE_STEP_MOVE_LINES:-1}"
    local text="$icon $title"
    if [ -n "$suffix" ]; then
        text="$text $suffix"
    fi
    printf '\033[%dA\r\033[2K%b%s%b\033[%dB\r' "$move_lines" "$color" "$text" "$ZHH_COLOR_RESET" "$move_lines"
}

zhh_step_start_spinner(){
    zhh_step_stop_spinner >/dev/null 2>&1 || true
    (
        local frame=0
        while true; do
            local dots=""
            case $((frame % 4)) in
                0) dots="" ;;
                1) dots="." ;;
                2) dots=".." ;;
                3) dots="..." ;;
            esac
            zhh_step_redraw "$ZHH_COLOR_BLUE$ZHH_COLOR_BOLD" "🔵" "$dots"
            sleep 0.4
            frame=$((frame+1))
        done
    ) &
    export ZHH_ACTIVE_STEP_SPINNER_PID=$!
}

zhh_step_stop_spinner(){
    local pid="${ZHH_ACTIVE_STEP_SPINNER_PID:-}"
    if [ -n "$pid" ]; then
        kill "$pid" > /dev/null 2>&1 || true
        wait "$pid" 2>/dev/null || true
        unset ZHH_ACTIVE_STEP_SPINNER_PID
    fi
}

zhh_step_done(){
    zhh_step_stop_spinner
    zhh_step_redraw "$ZHH_COLOR_GREEN$ZHH_COLOR_BOLD" "✅" "[DONE]"
    unset ZHH_ACTIVE_STEP_TITLE
    unset ZHH_ACTIVE_STEP_MOVE_LINES
}

zhh_step_fail(){
    local suffix="${1:-[FAILED]}"
    zhh_step_stop_spinner
    zhh_step_redraw "$ZHH_COLOR_RED$ZHH_COLOR_BOLD" "❌" "$suffix"
    unset ZHH_ACTIVE_STEP_TITLE
    unset ZHH_ACTIVE_STEP_MOVE_LINES
}

zhh_step_warn(){
    local suffix="${1:-[WARN]}"
    zhh_step_stop_spinner
    zhh_step_redraw "$ZHH_COLOR_YELLOW$ZHH_COLOR_BOLD" "⚠️" "$suffix"
    unset ZHH_ACTIVE_STEP_TITLE
    unset ZHH_ACTIVE_STEP_MOVE_LINES
}

zhh_cleanup_ui(){
    zhh_step_stop_spinner >/dev/null 2>&1 || true
    unset ZHH_ACTIVE_STEP_TITLE
    unset ZHH_ACTIVE_STEP_MOVE_LINES
}
