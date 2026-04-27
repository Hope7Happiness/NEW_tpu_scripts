source $ZHH_SCRIPT_ROOT/scripts/apply.sh
source $ZHH_SCRIPT_ROOT/scripts/sscript.sh
source $ZHH_SCRIPT_ROOT/scripts/auto.sh
source $ZHH_SCRIPT_ROOT/scripts/apply_pool.sh

zone_to_gs_name(){
    if [ -z "$1" ]; then
        echo -e "\033[31m[Internal Error] zone_to_gs_name requires a zone argument. Contact admin.\033[0m" >&2
        return 1
    fi

    for zones in us-central1 us-east1 us-east5 us-central2 asia-northeast1-b europe-west4; do
        if [[ $1 =~ $zones ]]; then
            # if zone is europe-west4, then directly return empty
            if [[ $zones == "europe-west4" ]]; then
                echo ""
                return 0
            fi
            echo $zones
            return 0
        fi
    done
    return 1
}

ckpt_to_gs(){
    path=$1
    # path: /kmh-nfs-us-mount/staging/<WHO>/PROJECT/other_parts
    # output: gs://kmh-gcp-us-central2/qiao_zhicheng_hanhong_files/PROJECT/other_parts

    if [ -z "$WHO" ]; then
        echo -e "\033[31m[Internal Error] WHO is not set.\033[0m" >&2
        return 1
    fi

    subpath=$(echo "$path" | sed "s|/kmh-nfs-ssd-us-mount/staging/${WHO}/||")
    # upd: grep zone from subpath.
    #     matched_zones = [z for z in ['us-central1', 'us-east1', 'us-east5', 'us-central2'] if z in path]
    # return matched_zones[0]

    # for zones in us-central1 us-east1 us-east5 us-central2 asia-northeast1-b; do
    #     if [[ $subpath =~ $zones ]]; then
    #         zone=$zones
    #         break
    #     fi
    # done
    zone=$(zone_to_gs_name $subpath)

    output=gs://kmh-gcp-$zone/$GS_STAGING_NAME/$subpath
    echo $output
}

# wandb_note_from_stagedir(){
#     stage_dir=$1
#     # else: read $stage_dir/.extra_args, grep notes=... until '
#     note_from_arg=$(cat $stage_dir/.extra_args 2>/dev/null | grep -oP -- "notes=\K[^'\"]+")
#     if [ ! -z "$note_from_arg" ]; then
#         echo "$note_from_arg"
#     else
#         cat $stage_dir/configs/remote_run_config.yml | grep -oP 'wandb_notes: \K.*' | head -n 1
#     fi
# }

wandb_id_from_logdir(){
    log_dir=$1
    cat $log_dir/output.log | grep -oP 'wandb: .*runs/\K[^\s]+' | head -n 1
}

is_run_like_command(){
    local cmd="${1:-}"
    [[ -z "$cmd" || "$cmd" == "q" || "$cmd" == "rr" || "$cmd" == "qrr" ]]
}

stage(){
    local stage_ret=0

    if [ -z "$PROJECT" ]; then
        zhh_error "PROJECT is not set."
        return 1
    fi

    if [ -z "$WHO" ]; then
        zhh_error "WHO is not set. Please set it to your username."
        zhh_warn "Use \`zhh help\` for more info."
        return 1
    fi

    STAGE_ROOT=/kmh-nfs-ssd-us-mount/staging/$WHO/$PROJECT
    NOW_STR=$(date +'%Y%m%d_%H%M%S')
    RND_STR=$(cat /dev/urandom | tr -cd 'a-f0-9' | head -c 8)
    GIT_STR=$(git -c safe.directory="$PWD" rev-parse --short HEAD 2>/dev/null || printf 'nogit')
    STAGE_DIR=$STAGE_ROOT/launch_${NOW_STR}_git${GIT_STR}_${RND_STR}

    zhh_set_stage_context "$STAGE_DIR"
    zhh_step_banner "Stage workspace" "" \
        "$(zhh_format_detail "stage dir" "$STAGE_DIR")"
    zhh_step_start_spinner

    if mkdir -p "$STAGE_DIR" 2>/dev/null && chmod 777 "$STAGE_DIR" 2>/dev/null; then
        # temporally patch
        rsync -a -O --exclude '.git' --exclude '.opencode' --exclude '__pycache__' --exclude '*.pyc' --exclude 'logs' --exclude 'wandb' --exclude='*.npz' . "$STAGE_DIR" && stage_ret=0 || stage_ret=$?
    else
        stage_ret=1
    fi
    if [ $stage_ret -ne 0 ] && [ ! -w "$STAGE_ROOT" ]; then
        zhh_muted_warn "Stage root is not writable by $(whoami); falling back to sudo."
        zhh_sudo mkdir -p "$STAGE_DIR"
        zhh_sudo chmod 777 "$STAGE_DIR"
        zhh_sudo rsync -a -O --exclude '.git' --exclude '.opencode' --exclude '__pycache__' --exclude '*.pyc' --exclude 'logs' --exclude 'wandb' --exclude='*.npz' . "$STAGE_DIR" && stage_ret=0 || stage_ret=$?
    fi
    # sudo rsync -a -O --exclude '.git' --exclude '.opencode' --exclude '__pycache__' --exclude '*.pyc' --exclude 'logs' --exclude 'wandb' . $STAGE_DIR
    if [ $stage_ret -eq 0 ]; then
        zhh_step_done
    else
        zhh_step_fail "[FAILED]"
    fi
    return $stage_ret
}

zkill(){
    kill_tpu $VM_NAME $ZONE && ret=0 || ret=$?
    # if ret is 9, then the tpu is preempted, deregister it
    if [ $ret -eq 9 ]; then
        echo -e "\033[33m[Info] TPU $VM_NAME in $ZONE is preempted. Deregistering...\033[0m"
        deregister_tpu $VM_NAME
        return 0
    fi
    killed_command
    # ask the user if want to dequeue
    if [ "$ZHH_SKIP_QUEUE_PROMPT" != "1" ] && ! queue_isempty; then
        read -p "Do you want to release a queue slot for $VM_NAME? (y/N) " yn
        if [[ "$yn" == "y" ]]; then
            release_queue
        fi
    fi
}

zkill_explicit(){
    local vm_name="$1"
    local zone="$2"
    local ret=0
    local had_vm_name=false
    local had_zone=false
    local old_vm_name=""
    local old_zone=""

    if [ -z "$vm_name" ] || [ -z "$zone" ]; then
        zhh_error "Usage: zhh kill <vm_name> <zone>"
        return 1
    fi

    if [ "${VM_NAME+x}" = "x" ]; then
        had_vm_name=true
        old_vm_name="$VM_NAME"
    fi
    if [ "${ZONE+x}" = "x" ]; then
        had_zone=true
        old_zone="$ZONE"
    fi

    VM_NAME="$vm_name"
    ZONE="$zone"
    zkill && ret=0 || ret=$?

    if $had_vm_name; then
        VM_NAME="$old_vm_name"
    else
        unset VM_NAME
    fi
    if $had_zone; then
        ZONE="$old_zone"
    else
        unset ZONE
    fi

    return $ret
}

killer_trap(){
    zhh_cleanup_ui
    echo -e "\n\033[33m[Info] Caught interrupt signal. Running kill...\033[0m"
    zkill
    # if FAST_DEBUG is set, run again
    if [ ! -z "$FAST_DEBUG" ]; then
        echo -e "\033[33m[Info] FAST_DEBUG is set. Running job again...\033[0m"
        cd $HERE
        zhh_debug "now at $(pwd)"
        trap - INT # reset trap, avoid multiple traps stacking
        zhh_debug "re-running job in 5s (Ctrl+C now if you do not want)..."
        sleep 5
        zrun
    else
        echo -e "\033[33m[Info] Not re-running job since FAST_DEBUG is not set. If you want to enable fast debug, set FAST_DEBUG=1 and re-run the command.\033[0m"
    fi
    exit 0
}


run_job(){
    # args: $1=STAGE_DIR, $2...=extra args (passed to main.py)
    HERE=$PWD
    local patch_ret=0
    local kill_ret=0

    STAGE_DIR=$1
    LOG_ROOT=$STAGE_DIR/logs

    NOW_STR=$(date +'%Y%m%d_%H%M%S')
    RND_STR=$(cat /dev/urandom | tr -cd 'a-f0-9' | head -c 8)
    exist_logs=$(ls "$LOG_ROOT" 2>/dev/null | grep -c '^log[0-9]\+_' || true)
    cur_log_id=$((exist_logs+1))
    LOG_DIR=$LOG_ROOT/log${cur_log_id}_${NOW_STR}_VM${VM_NAME}_Z${ZONE}_${RND_STR}
    PRE_RUN_LOG_FILE=""

    sudo mkdir -p $LOG_DIR
    sudo chmod 777 $LOG_DIR
    PRE_RUN_LOG_FILE="$LOG_DIR/run_prepare.log"
    : > "$PRE_RUN_LOG_FILE"
    zhh_box_section "Prepare runtime"
    zhh_kv "run log dir" "$LOG_DIR"
    if [ -n "$ZHH_CENTER_RUN_ID" ]; then
        python3 "$ZHH_SCRIPT_ROOT/tpu_center/cli.py" worker-log-dir --run-id "$ZHH_CENTER_RUN_ID" --log-dir "$LOG_DIR" || true
    fi

    # EXTRA_ARGS should be a list
    local EXTRA_ARGS=()

    # extra args is $@[2:]
    # EXTRA_ARGS=("${EXTRA_ARGS[@]}" "${@:2}")
    for arg in "${@:2}"; do
        if [[ "$arg" =~ [[:alnum:]_] ]]; then # only add useful args
            EXTRA_ARGS+=("$arg")
        fi
    done

    # check whether a checkpoint exists
    printf '[INFO] finding checkpoints...\n' >> "$PRE_RUN_LOG_FILE"
    for check_id in $(seq $((cur_log_id-1)) -1 1); do
        LAST_LOG_DIR=$(ls "$LOG_ROOT" 2>/dev/null | grep "^log${check_id}_" | tail -n 1)
        if [ -z "$LAST_LOG_DIR" ]; then
            continue
        fi
        
        # convert to gs bucket
        GS_DIR=$(ckpt_to_gs $LOG_ROOT/$LAST_LOG_DIR)
        printf '[INFO] |___checking previous log dir %s -> %s\n' "$LOG_ROOT/$LAST_LOG_DIR" "$GS_DIR" >> "$PRE_RUN_LOG_FILE"

        # parse GS_DIR as gs://kmh-gcp-$zone/$GS_STAGING_NAME/$subpath
        # subpath = '/'.join(GS_DIR.split('/')[4:])
        # zone = GS_DIR.split('/')[2].split('-')[-1]
        subpath=$(echo "$GS_DIR" | sed 's|gs://kmh-gcp-[^/]\+/\([^/]\+\)/\(.*\)|\2|')
        zone=$(echo "$GS_DIR" | sed 's|gs://kmh-gcp-\([^/]\+\)/.*|\1|')

        correct_gs_zone=$(zone_to_gs_name $ZONE)
        
        # check if checkpoints exist in the gs bucket
        if gsutil ls $GS_DIR/checkpoint_* > /dev/null; then
            printf '[INFO]   ===>found previous checkpoint dir %s in gs %s\n' "$LOG_ROOT/$LAST_LOG_DIR" "$GS_DIR" >> "$PRE_RUN_LOG_FILE"

            if [ "$zone" != "$correct_gs_zone" ]; then
                # if dest already exists, skip copy
                if gsutil ls gs://kmh-gcp-$correct_gs_zone/$GS_STAGING_NAME/$subpath > /dev/null; then
                    printf '[INFO]   ===>checkpoint already copied to correct gs zone gs://kmh-gcp-%s/%s/%s, skip copy\n' "$correct_gs_zone" "$GS_STAGING_NAME" "$subpath" >> "$PRE_RUN_LOG_FILE"
                else
                    printf '[INFO]   ===>copying checkpoint from gs://kmh-gcp-%s/%s/%s to gs://kmh-gcp-%s/%s/%s\n' "$zone" "$GS_STAGING_NAME" "$subpath" "$correct_gs_zone" "$GS_STAGING_NAME" "$subpath" >> "$PRE_RUN_LOG_FILE"

                    # we only copy the last checkpoint, sort by numerical order, and copy the last one
                    last_ckpt=$(
                        gsutil ls "$GS_DIR"/checkpoint_* |
                        sed 's|.*/checkpoint_||' |
                        cut -d/ -f1 |
                        sort -n |
                        tail -n 1
                    )
                    printf '[INFO]   ===>copying last checkpoint checkpoint_%s\n' "$last_ckpt" >> "$PRE_RUN_LOG_FILE"

                    zhh_run_logged_command "$PRE_RUN_LOG_FILE" gcloud storage cp -r gs://kmh-gcp-$zone/$GS_STAGING_NAME/$subpath/checkpoint_$last_ckpt gs://kmh-gcp-$correct_gs_zone/$GS_STAGING_NAME/$subpath/checkpoint_$last_ckpt
                fi
            fi

            EXTRA_ARGS+=("--config.load_from=gs://kmh-gcp-$correct_gs_zone/$GS_STAGING_NAME/$subpath")
            # EXTRA_ARGS+=("--config.load_from=$LOG_ROOT/$LAST_LOG_DIR")
            break
        fi
        printf '[INFO] no checkpoints found in %s\n' "$LOG_ROOT/$LAST_LOG_DIR" >> "$PRE_RUN_LOG_FILE"
    done;

    printf '[INFO] finding past wandb runs...\n' >> "$PRE_RUN_LOG_FILE"
    for check_id in $(seq $((cur_log_id-1)) -1 1); do
        LAST_LOG_DIR=$(ls "$LOG_ROOT" 2>/dev/null | grep "^log${check_id}_" | tail -n 1)
        if [ -z "$LAST_LOG_DIR" ]; then
            continue
        fi
        
        # convert to gs bucket
        WANDB_ID=$(wandb_id_from_logdir $LOG_ROOT/$LAST_LOG_DIR)
        if [ ! -z "$WANDB_ID" ]; then
            printf '[INFO]   ===>found previous wandb run id %s from %s\n' "$WANDB_ID" "$LOG_ROOT/$LAST_LOG_DIR" >> "$PRE_RUN_LOG_FILE"
            EXTRA_ARGS+=("--config.wandb_resume_id=$WANDB_ID")
            break
        fi
    done;

    DBG_COMMANDS="ls $CONDA_PY_PATH"
    py_path=$CONDA_PY_PATH
    # if VM_NAME contains v6, don't use conda
    if use_v6_script $VM_NAME; then
        py_path="python"
        DBG_COMMANDS="which python"
    fi

    # kill anyway
    DBG_COMMANDS="$DBG_COMMANDS && sudo rm -rf /tmp/*tpu*"

    json_file=$(get_service_json)
    if [ $? -ne 0 ]; then
        return 1
    fi
    py_path="GOOGLE_APPLICATION_CREDENTIALS=$json_file $py_path"

    # if EXTRA_ARGS exists:
    EXTRA_ARGS_STR=""
    if [ ! -z "$EXTRA_ARGS" ]; then
        EXTRA_ARGS_STR=$(printf "'%s' " "${EXTRA_ARGS[@]}")
    fi

    if [ -f "$STAGE_DIR/补.sh" ]; then
        PATCH_LOG_FILE="$LOG_DIR/patch_script.log"
        : > "$PATCH_LOG_FILE"
        zhh_step_banner "Run patch script" "$PATCH_LOG_FILE"
        zhh_step_start_spinner
        # bash $STAGE_DIR/补.sh && ret=0 || ret=$?

        # two cases:
        # 1. "gcloud" is found in 补.sh, which means it is doing some gcloud command, we should run it on local machine
        if grep -q "gcloud" $STAGE_DIR/补.sh; then
            zhh_run_logged_command "$PATCH_LOG_FILE" bash "$STAGE_DIR/补.sh" && patch_ret=0 || patch_ret=$?
        else
            zhh_run_logged_command "$PATCH_LOG_FILE" "$CUSTOM_GCLOUD_EXE" compute tpus tpu-vm ssh "$VM_NAME" --zone "$ZONE" --worker=all --command "bash $STAGE_DIR/补.sh" && patch_ret=0 || patch_ret=$?
        fi

        if [ $patch_ret -ne 0 ]; then
            zhh_step_fail "[FAILED]"
            zhh_note "Log: $PATCH_LOG_FILE"
            return $patch_ret
        fi
        zhh_step_done
    fi

    # kill anyway
    KILL_LOG_FILE="$LOG_DIR/kill_tpu.log"
    : > "$KILL_LOG_FILE"
    zhh_step_banner "Clear TPU processes" "$KILL_LOG_FILE"
    zhh_step_start_spinner
    export ZHH_KILL_TPU_LOG_FILE="$KILL_LOG_FILE"
    export ZHH_SKIP_QUEUE_PROMPT=1
    zkill && kill_ret=0 || kill_ret=$?
    unset ZHH_KILL_TPU_LOG_FILE
    unset ZHH_SKIP_QUEUE_PROMPT
    if [ $kill_ret -eq 0 ]; then
        zhh_step_done
    elif [ $kill_ret -eq 9 ]; then
        zhh_step_warn "[PREEMPTED]"
        zhh_note "Log: $KILL_LOG_FILE"
    else
        zhh_step_fail "[FAILED]"
        zhh_note "Log: $KILL_LOG_FILE"
    fi


    # COMMAND="ls /foo/bar | sudo tee -a $LOG_DIR/output.log"
    COMMAND="$py_path main.py --workdir=$LOG_DIR --mode=remote_run --config=configs/load_config.py:remote_run $EXTRA_ARGS_STR 2>&1"

    # if main.py doesn't exist, check if main.sh exists
    if [ ! -f "$STAGE_DIR/main.py" ] && [ -f "$STAGE_DIR/main.sh" ]; then
        printf '[INFO] main.py not found, using main.sh instead.\n' >> "$PRE_RUN_LOG_FILE"
        COMMAND="bash main.sh $LOG_DIR $EXTRA_ARGS_STR 2>&1"
    fi


    # register command
    log_command "$COMMAND"

    # report runtime log dir to current HTTP server (if running under server.py)
    if [ ! -z "$ZHH_SERVER_URL" ] && [ ! -z "$ZHH_JOB_ID" ]; then
        curl -s -m3 -X POST "$ZHH_SERVER_URL/job-log-dir/$ZHH_JOB_ID" \
            --data-urlencode "log_dir=$LOG_DIR" > /dev/null || zhh_warn "Failed to report runtime log dir to task server."
    fi

    zhh_box_section "Launch training"
    if [ -f "$STAGE_DIR/main.py" ]; then
        zhh_info "Running main.py. Good luck!"
    else
        zhh_info "Running main.sh. Good luck!"
    fi
    zhh_hr "-"
    (echo "$COMMAND"; echo ========; echo; ) > $LOG_DIR/output.log


    # trap a ^C signal
    trap - INT # reset trap, avoid multiple traps stacking
    trap killer_trap INT

    cd $STAGE_DIR && \
    $CUSTOM_GCLOUD_EXE compute tpus tpu-vm ssh $VM_NAME --zone $ZONE --worker=all --command "$DBG_COMMANDS && cd $STAGE_DIR && $COMMAND" 2>&1 | stdbuf -oL -eL sudo tee -a $LOG_DIR/output.log

    status=${PIPESTATUS[0]}   # this get the return code of gcloud
    unset FAST_DEBUG # when job preempted/finished, reset FAST_DEBUG

    trap - INT # reset trap

    [ $status -eq 0 ] || (
        echo -e "\033[31m[Error] Job failed. Check logs in $LOG_DIR/output.log\033[0m" >&2
        fail_command

        # now, in anyway, we deregister it
        echo -e "\033[33m[Info] Releasing TPU $VM_NAME in $ZONE...\033[0m"
        deregister_tpu $VM_NAME

        # # this is a temporal fix, eventually we figure it out
        # if [[ ! "$VM_NAME" =~ kangyang ]]; then
        #     echo "[INFO] Releasing TPU $VM_NAME in $ZONE..."
        #     deregister_tpu $VM_NAME
        # fi

        # NOTE: default, we don't release queue slot on failure
        return 7
    ) && (
        echo -e "\033[32m[Success] Job finished (maybe failed). Check logs in $LOG_DIR/output.log\033[0m" >&2
        success_command
        cd $HERE

        # release a queue slot
        echo "[INFO] Releasing a queue slot..."
        release_queue

        # # if the card is not *kangyang*, release it
        # if [[ ! "$VM_NAME" =~ kangyang ]]; then
        #     echo "[INFO] Releasing TPU $VM_NAME in $ZONE..."
        #     deregister_tpu $VM_NAME
        # fi


        # now, in anyway, we deregister it
        echo -e "\033[33m[Info] Releasing TPU $VM_NAME in $ZONE...\033[0m"
        deregister_tpu $VM_NAME
    )
}

while_run(){
    STAGE_DIR=$1
    # extra args are $2...
    EXTRA_ARGS=("${@:2}")

    log_stage_dir "$STAGE_DIR"
    # run_job $STAGE_DIR "${EXTRA_ARGS[@]}" && ret=0 || ret=$?

    # if ret==7 (job failed), auto-check card status, if bad, re-setup env and re-run

    ret=7

    # the logic here is quite complicated, I am not sure whether it can be cleaned
    while [ $ret -eq 7 ]; do

        get_and_setup_tpu $VM_NAME $ZONE && \
        register_tpu && \
        run_job $STAGE_DIR "${EXTRA_ARGS[@]}" \
        && ret=0 || ret=$?
        zhh_debug "Initial run returned $ret"

        if [ $ret -eq 0 ]; then
            echo -e "\033[32m[Info] Job finished successfully (in one trial! lucky you!).\033[0m"
            return 0
        fi

        if [ $ret -eq 42 ]; then
            zhh_warn "Current TPU could not be prepared. Re-selecting another card."
        else

            echo -e "\033[31m[Error] Job failed, first wait for a moment (feel free to ^C if you are here)...\033[0m"
            # sleep 600
            sleep 60

            echo "[INFO] Checking TPU status..."
            # if has tpu and the return code is not 42
            # if has_tpu $VM_NAME $ZONE; then
            # if ! is_preempted $VM_NAME $ZONE; then
            if has_tpu $VM_NAME $ZONE; then
                # note: better make the code more likely to enter this branch
                # this will avoid infinite loop

                # check if there is GRPC error
                # if grep -q "This TPU is going through a maintenance event, and might be unavailable" $LOG_DIR/output.log; then
                #     echo -e "\033[33m[Info] Found maintenance event in logs, this TPU is no longer usable. Aborted.\033[0m"
                #     return 1
                
                # case 1: exist output.log
                if [ -f "$LOG_DIR/output.log" ]; then
                    zhh_debug "log found at $LOG_DIR/output.log, checking logs for errors..."

                    if grep -q '\[/usr/bin/ssh\] exited with return code \[255\]' $LOG_DIR/output.log || grep -q "Terminating process because the coordinator detected missing heartbeats." $LOG_DIR/output.log; then
                        echo -e "\033[33m[Info] Found GRPC/heartbeat error in logs, will re-setup env and re-run.\033[0m"
                        # sleep 60
                        # kill_tpu $VM_NAME $ZONE
                        zhh_debug "Re-running job..."
                        get_and_setup_tpu $VM_NAME $ZONE && \
                        register_tpu && \
                        kill_tpu $VM_NAME $ZONE && \
                        sleep 10 && \
                        kill_tpu $VM_NAME $ZONE && \
                        run_job $STAGE_DIR "${EXTRA_ARGS[@]}" \
                        && ret=0 || ret=$?
                        zhh_debug "Re-run (for grpc) returned $ret"
                # elif grep -q "Fatal Python error: Aborted" $LOG_DIR/output.log; then
                #     echo -e "\033[33m[Info] Found Segfault in logs, will wait and re-run...\033[0m"
                #     echo "[Debug] Re-running job..."
                #     kill_tpu $VM_NAME $ZONE && \
                #     echo "[Debug] Sleep for a while before re-running..." && \
                #     sleep 300 && \
                #     run_job $STAGE_DIR "${EXTRA_ARGS[@]}" \
                #     && ret=0 || ret=$?
                #     echo "[Debug] Re-run (for segfault) returned $ret"
                    elif grep -q "(core dumped)" $LOG_DIR/output.log || grep -q "Command execution on worker 0 failed with exit status 134" $LOG_DIR/output.log || grep -q "UNKNOWN: TPU initialization failed:" $LOG_DIR/output.log || grep -q "ABORTED: The TPU is already in use by process" $LOG_DIR/output.log; then
                        echo -e "\033[33m[Info] Our job is killed by others. Will change card and re-run...\033[0m"
                        deregister_tpu $VM_NAME $ZONE
                        ret=42
                    else
                        echo -e "\033[32m[Info] Card status looks good, then it is probably a code bug. Please fix it and re-run.\033[0m"
                        return 1
                    fi
                else
                    echo -e "Log file not found. This means the environment setup failed."
                    echo -e "\033[33m[Info] Will change card and re-run...\033[0m"
                    deregister_tpu $VM_NAME $ZONE
                    ret=42
                fi
            else
                echo -e "\033[33m[Info] Card $VM_NAME in $ZONE is PREEMPTED, will re-apply and re-run.\033[0m"
                # zhh: resumes with an auto card
                # reset EXIT trap
                trap - EXIT
                trap 'zhh_cleanup_ui' EXIT
                accel_arg=$(get_accelerator_args $VM_NAME)
                # split by '-'
                type_part=$(echo $accel_arg | cut -d'-' -f1)
                size_part=$(echo $accel_arg | cut -d'-' -f2)
                export VM_NAME="auto${type_part}"
                export TPU_TYPES="$size_part"
                export ZONE=$ZONE_INITIAL
                auto_select && \
                log_stage_dir "$STAGE_DIR" && \
                get_and_setup_tpu $VM_NAME $ZONE && \
                register_tpu && \
                run_job $STAGE_DIR "${EXTRA_ARGS[@]}" \
                && ret=0 || ret=$?
                zhh_debug "Re-run with auto-select returned $ret"
            fi
        fi

        while [ $ret -eq 42 ]; do
            zhh_note "[INFO] Re-doing auto-select..."

            accel_arg=$(get_accelerator_args $VM_NAME)
            # split by '-'
            type_part=$(echo $accel_arg | cut -d'-' -f1)
            size_part=$(echo $accel_arg | cut -d'-' -f2)
            export VM_NAME="auto${type_part}"
            export TPU_TYPES="$size_part"
            export ZONE=$ZONE_INITIAL
            auto_select && \
            log_stage_dir "$STAGE_DIR" && \
            get_and_setup_tpu $VM_NAME $ZONE && \
            register_tpu && \
            run_job $STAGE_DIR "${EXTRA_ARGS[@]}" \
            && ret=0 || ret=$?
            zhh_debug "Re-doing auto-select returned $ret"
        done
    done

    return $ret
}

zget(){
    # get tpu only
    get_and_setup_tpu $VM_NAME $ZONE && \
    register_tpu
}

zrun(){
    # extra args are $@
    EXTRA_ARGS=("$@")

    starting_command # avoid multiple jobs starting together

    # staging to $STAGE_DIR
    stage || return 1
    log_stage_dir "$STAGE_DIR"

    # if EXTRA_ARGS exists, write to a file in STAGE_DIR
    # if [ ! -z "$EXTRA_ARGS" ]; then
    #     (printf "'%s' " "${EXTRA_ARGS[@]}" | sudo tee $STAGE_DIR/.extra_args) > /dev/null
    # fi

    # prepare TPU
    # get_and_setup_tpu $VM_NAME $ZONE && \
    # register_tpu && \
    while_run $STAGE_DIR "${EXTRA_ARGS[@]}"
}

zsubmit(){
    local priority="${ZHH_PRIORITY:-0}"
    local extra_args=()
    local missing=()

    while [ $# -gt 0 ]; do
        case "$1" in
            -p|--priority)
                if [ -z "$2" ]; then
                    zhh_error "Usage: zhh submit [--priority N] [main.py args...]"
                    return 1
                fi
                priority="$2"
                shift 2
                ;;
            --priority=*)
                priority="${1#--priority=}"
                shift
                ;;
            --)
                shift
                while [ $# -gt 0 ]; do
                    extra_args+=("$1")
                    shift
                done
                ;;
            *)
                extra_args+=("$1")
                shift
                ;;
        esac
    done

    if [[ ! "$priority" =~ ^-?[0-9]+$ ]]; then
        zhh_error "Priority must be an integer: $priority"
        return 1
    fi

    export WHO="${WHO:-$WECODE_USER}"
    [ -n "$PROJECT" ] || missing+=("PROJECT")
    [ -n "$WANDB_API_KEY" ] || missing+=("WANDB_API_KEY")
    [ -n "$TPU_TYPES" ] || missing+=("TPU_TYPES")
    [ -n "$VM_NAME" ] || missing+=("VM_NAME")
    [ -n "$WHO" ] || missing+=("WHO")
    if [ ${#missing[@]} -ne 0 ]; then
        zhh_error "Missing required environment variables: ${missing[*]}"
        zhh_warn "Set them in .ka or your shell and submit again."
        return 1
    fi

    stage || return 1
    python3 "$ZHH_SCRIPT_ROOT/tpu_center/cli.py" submit-staged \
        --stage-dir "$STAGE_DIR" \
        --priority "$priority" \
        --cwd "$PWD" \
        -- "${extra_args[@]}"
}

zcenter_worker(){
    local run_id="$1"
    local stage_dir="$2"
    local vm_name="$3"
    local zone="$4"
    local ret=1

    set +e

    if [ -z "$run_id" ] || [ -z "$stage_dir" ] || [ -z "$vm_name" ] || [ -z "$zone" ]; then
        zhh_error "Usage: zhh center-worker <run_id> <stage_dir> <vm_name> <zone> [-- main.py args...]"
        return 1
    fi
    shift 4
    if [ "${1:-}" = "--" ]; then
        shift
    fi

    export ZHH_CENTER_RUN_ID="$run_id"
    export VM_NAME="$vm_name"
    export ZONE="$zone"
    export ZHH_SKIP_QUEUE_PROMPT=1
    zhh_set_stage_context "$stage_dir"
    zhh_center_stage "Worker starting"
    log_stage_dir "$stage_dir" || zhh_warn "Failed to update legacy TPU status for $VM_NAME. Continuing."

    zcenter_prepare_assigned_tpu "$VM_NAME" "$ZONE"
    ret=$?
    if [ $ret -eq 0 ]; then
        zhh_center_stage "Register TPU"
        register_tpu
        ret=$?
    fi
    if [ $ret -eq 0 ]; then
        zhh_center_stage "Run job"
        run_job "$stage_dir" "$@"
        ret=$?
    fi
    zhh_center_stage "Worker finished"
    python3 "$ZHH_SCRIPT_ROOT/tpu_center/cli.py" worker-finished --run-id "$run_id" --exit-code "$ret" || true
    return $ret
}

zcenter_prepare_assigned_tpu(){
    local vm_name="$1"
    local zone="$2"
    local ret=0

    zhh_box_section "Prepare assigned TPU"
    zhh_kv "tpu" "$vm_name @ $zone"
    has_tpu "$vm_name" "$zone" || {
        zhh_warn "Assigned TPU is not READY. Returning it to center."
        return 2
    }
    tpu_in_use "$vm_name" "$zone" || {
        zhh_warn "Assigned TPU is busy. Returning it to center."
        return 3
    }
    export ZHH_SETUP_TRIAL=1
    setup_tpu "$vm_name" "$zone"
    ret=$?
    unset ZHH_SETUP_TRIAL
    if [ $ret -ne 0 ]; then
        zhh_warn "Assigned TPU setup failed with ret=$ret. Returning it to center."
        return $ret
    fi
    zhh_box_success "Assigned TPU $vm_name @ $zone is ready."
    return 0
}

zcenter_probe(){
    local vm_name="$1"
    local zone="$2"
    local ret=0

    if [ -z "$vm_name" ] || [ -z "$zone" ]; then
        zhh_error "Usage: zhh center-probe <vm_name> <zone>"
        return 1
    fi

    export VM_NAME="$vm_name"
    export ZONE="$zone"
    has_tpu "$VM_NAME" "$ZONE" || {
        echo "TPU is not READY: $VM_NAME @ $ZONE"
        return 2
    }
    tpu_in_use "$VM_NAME" "$ZONE" || {
        echo "TPU device is busy or unavailable: $VM_NAME @ $ZONE"
        return 3
    }
    echo "TPU status ready and idle: $VM_NAME @ $ZONE"
    return 0
}

zrerun(){
    # check if in staging dir
    if [[ ! $(pwd) =~ /kmh-nfs-ssd-us-mount/staging/ ]]; then
        echo -e "\033[31m[Error] You are NOT in a staging directory. Aborted.\033[0m" >&2
        return 1
    fi

    log_stage_dir "$(pwd)"

    # parse "WHO" from pwd
    export WHO=$(echo $(pwd) | cut -d'/' -f4)

    starting_command # avoid multiple jobs starting together
    zhh_box_section "Reuse staged workspace"
    zhh_kv "stage path" "$(pwd)"
    zhh_set_stage_context "$(pwd)"
    zhh_kv "prep logs" "$ZHH_PREP_LOG_DIR"

    # check if .extra_args exists
    # EXTRA_ARGS=""
    if [ -f .extra_args ]; then
        echo -e "\033[31m[WARNING] Exist .extra_args found in current directory. This is no longer supported, it will have no effect." >&2
        # read -a EXTRA_ARGS < <(cat .extra_args) || true
        # echo "[INFO] Using extra args: ${EXTRA_ARGS[@]}"
    else
        echo "[INFO] No extra args found."
    fi
    
    # prepare TPU
    # get_and_setup_tpu $VM_NAME $ZONE && \
    # register_tpu && \
    while_run "$(pwd)" "${EXTRA_ARGS[@]}"
}

zqueue(){
    EXTRA_ARGS=("$@")

    # confirm
    read -p "Queue the job on $VM_NAME, with args $EXTRA_ARGS... ? (y/N) " yn
    if [ "$yn" != "y" ]; then
        echo "[INFO] Aborted."
        return 1
    fi

    # staging to $STAGE_DIR
    stage || return 1

    # if EXTRA_ARGS exists, write to a file in STAGE_DIR
    # if [ ! -z "$EXTRA_ARGS" ]; then
    #     (printf "'%s' " "${EXTRA_ARGS[@]}" | sudo tee $STAGE_DIR/.extra_args) > /dev/null
    # fi

    # if good_tpu $VM_NAME $ZONE && queue_isempty $VM_NAME && ! has_failure $VM_NAME; then
    #     echo -e "\033[32m[INFO] TPU VM $VM_NAME is already free and no jobs in queue. Directly running...\033[0m"
    #     setup_tpu $VM_NAME $ZONE && \
    #     while_run $STAGE_DIR "${EXTRA_ARGS[@]}" && ret=0 || ret=$?
    #     return $ret
    # fi
    if has_failure $VM_NAME; then
        echo -e "\033[33m[Info] TPU VM $VM_NAME has failure. Will enter queue state...\033[0m"
    elif good_tpu $VM_NAME $ZONE && queue_isempty $VM_NAME; then
        echo -e "\033[32m[INFO] TPU VM $VM_NAME is already free and no jobs in queue. Directly running...\033[0m"
        # setup_tpu $VM_NAME $ZONE && \
        while_run $STAGE_DIR "${EXTRA_ARGS[@]}" && ret=0 || ret=$?
        return $ret
    fi

    queue_job $STAGE_DIR && \
    # get_and_setup_tpu $VM_NAME $ZONE && \
    # register_tpu && \
    while_run $STAGE_DIR "${EXTRA_ARGS[@]}"
}

zqueue_rerun(){
    # check if in staging dir
    if [[ ! $(pwd) =~ /kmh-nfs-ssd-us-mount/staging/ ]]; then
        echo -e "\033[31m[Error] You are NOT in a staging directory. Aborted.\033[0m" >&2
        return 1
    fi

    if [ -f .extra_args ]; then
        echo -e "\033[31m[WARNING] Exist .extra_args found in current directory. This is no longer supported, it will have no effect." >&2
        # read -a EXTRA_ARGS < <(cat .extra_args) || true
        # echo "[INFO] Using extra args: ${EXTRA_ARGS[@]}"
    else
        echo "[INFO] No extra args found."
    fi

    # confirm
    read -p "Queue the job on $VM_NAME, with args $EXTRA_ARGS... ? (y/N) " yn
    if [ "$yn" != "y" ]; then
        echo "[INFO] Aborted."
        return 1
    fi

    # staging to $STAGE_DIR
    stage || return 1

    # if EXTRA_ARGS exists, write to a file in STAGE_DIR
    # if [ ! -z "$EXTRA_ARGS" ]; then
    #     (printf "'%s' " "${EXTRA_ARGS[@]}" | sudo tee $STAGE_DIR/.extra_args) > /dev/null
    # fi

    if good_tpu $VM_NAME $ZONE && queue_isempty $VM_NAME && ! has_failure $VM_NAME; then
        echo -e "\033[32m[INFO] TPU VM $VM_NAME is already free and no jobs in queue. Directly running...\033[0m"
        # setup_tpu $VM_NAME $ZONE && \
        while_run $STAGE_DIR "${EXTRA_ARGS[@]}" && ret=0 || ret=$?
        return $ret
    fi

    queue_job $STAGE_DIR && \
    # get_and_setup_tpu $VM_NAME $ZONE && \
    # register_tpu && \
    while_run $STAGE_DIR "${EXTRA_ARGS[@]}"
}

zqueue_pop(){
    # release a queue slot
    get_and_setup_tpu $VM_NAME $ZONE && \
    register_tpu && \
    release_queue
}

run_matmul(){
    # use matmul to keep a TPU busy
    get_and_setup_tpu $VM_NAME $ZONE

    DBG_COMMANDS="ls $CONDA_PY_PATH"
    py_path=$CONDA_PY_PATH
    # if VM_NAME contains v6, don't use conda
    if use_v6_script $VM_NAME; then
        py_path="python"
        DBG_COMMANDS="which python"
    fi

    MATMUL_SCRIPT="import jax as j,time as t;from flax.jax_utils import replicate as e;p=j.numpy;r=j.random;k=r.PRNGKey(0);N=3<<14;_T=e(r.normal(k,(N,N)));__=j.pmap(lambda _: _.T@_/p.linalg.norm(_@_.T));exec('while True: (__(_T), t.sleep(0.5))')"


    COMMAND="$py_path -c \"$MATMUL_SCRIPT\" 2>&1"
    log_command "$COMMAND"
    # echo "This is going to stuck. Use this to kill: " "gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE --worker=all --command=\"ps -ef | grep python | grep linalg | grep -v grep | awk '{print \\\"kill -9 \\\" \\\$2}' | sh\""
    gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE --worker=all --command "$DBG_COMMANDS && $COMMAND" # never ends
}

zstatus(){
    # the original "zzz"

    # joke
    python3 -c 'import pyjokes; print(pyjokes.get_joke())' || echo 'Install pyjokes for a joke!'

    echo -e '\n🤓 ready to start your day?';

    show_all_tpu_status
    echo
    show_queue_status
}

zwhat(){
    COMMON_ERR_MSG="\033[31m[Error] Please use '--all' to show all tpu status, or use the \$VM_NAME env var to show current tpu status.\033[0m"

    if [ ! -z "$1" ]; then
        if [[ "$1" =~ .*all.* ]]; then
            res=$(list_tpus)
            VM_NAMES=($(echo "$res" | awk '{print $1}'))
            ZONES=($(echo "$res" | awk '{print $2}'))
        else
            echo -e $COMMON_ERR_MSG
            return 1
        fi
    else
        if [ -z "$VM_NAME" ]; then
            echo -e $COMMON_ERR_MSG
            return 1
        fi
        VM_NAMES=($VM_NAME)
        ZONES=($ZONE)
    fi

    for i in "${!VM_NAMES[@]}"; do
        VM_NAME=${VM_NAMES[$i]}
        ZONE=${ZONES[$i]}
        echo -e "\n=== TPU VM: $VM_NAME (zone: $ZONE) ==="
        # if ZONE is *INTERNAL*
        if [[ "$ZONE" =~ INTERNAL ]]; then
            echo -e "\033[33m[Internal Error] Zone is unset. Contact ZHH\033[0m"
        else
            out=$(good_tpu_verbose $VM_NAME $ZONE)
            echo -e "$out"
            # grep TPU VM \w+ is *\. 
            # status=$(echo "$out" | sed -oP 'TPU VM \w+ is (.*)\.')
            status=$(echo "$out" | sed -n 's/.*TPU VM [^\ ]\+ is \([^\\]*\)\..*/\1/p')
            log_tpu_check_result "$status"
        fi
    done;
}


zdelete(){
    COMMON_ERR_MSG="\033[31m[Error] Please use '--all' to deregister all bad tpus, or use the \$VM_NAME env var to de-register current tpu\033[0m"

    if [ ! -z "$1" ]; then
        if [[ "$1" =~ .*all.* ]]; then
            res=$(list_tpus)
            VM_NAMES=($(echo "$res" | awk '{print $1}'))
            ZONES=($(echo "$res" | awk '{print $2}'))
        else
            echo -e $COMMON_ERR_MSG
            return 1
        fi
    else
        if [ -z "$VM_NAME" ]; then
            echo -e $COMMON_ERR_MSG
            return 1
        fi

        VM_NAMES=($VM_NAME)
        ZONES=($ZONE)
    fi

    for i in "${!VM_NAMES[@]}"; do
        VM_NAME=${VM_NAMES[$i]}
        ZONE=${ZONES[$i]}
        # if ! get_tpu_check_result $VM_NAME | grep -q "deleted" && ! get_tpu_status $VM_NAME | grep -q "CREATING"; then
        if (
            get_tpu_status $VM_NAME | grep -q "CREATING" && ! get_tpu_check_result $VM_NAME | grep -q "ready" && ! tpu_has_command $VM_NAME \
            || get_tpu_check_result $VM_NAME | grep -q "deleted"
        ); then
            deregister_tpu $VM_NAME
            echo -e "\033[32m[Info] Deregistered TPU $VM_NAME\033[0m"
        else
            echo -e "\033[33m[Info] TPU $VM_NAME is not deleted, skip deregister.\033[0m"
        fi
    done;
}

zlogin(){
    json_file=$(get_service_json)
    gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE --worker=all --command="gcloud auth activate-service-account --key-file=$json_file"
}

zclean(){
    # try KILL all FAILED cards
    res=$(list_tpus)
    VM_NAMES=($(echo "$res" | awk '{print $1}'))
    ZONES=($(echo "$res" | awk '{print $2}'))
    for i in "${!VM_NAMES[@]}"; do
        VM_NAME=${VM_NAMES[$i]}
        ZONE=${ZONES[$i]}
        if has_failure $VM_NAME; then
            echo -e "\033[33m[Info] Found failed TPU $VM_NAME in $ZONE, trying to kill...\033[0m"
            # if VM_NAME doesn't contain kangyang, then deregister it
            if [[ ! "$VM_NAME" =~ kangyang ]]; then
                # kill_tpu $VM_NAME $ZONE || true
                deregister_tpu $VM_NAME
                echo -e "\033[32m[Info] Deregistered TPU $VM_NAME\033[0m"
            else
                kill_tpu $VM_NAME $ZONE && killed_command  || deregister_tpu $VM_NAME
            fi
        fi
    done;
}

check_config_sanity(){
    local strict_run=false
    local missing=()

    if is_run_like_command "$ZHH_MAIN_COMMAND"; then
        strict_run=true
    fi

    export WHO="${WHO:-$WECODE_USER}"

    if $strict_run; then
        [ -n "$PROJECT" ] || missing+=("PROJECT")
        [ -n "$WANDB_API_KEY" ] || missing+=("WANDB_API_KEY")
        [ -n "$TPU_TYPES" ] || missing+=("TPU_TYPES")
        [ -n "$VM_NAME" ] || missing+=("VM_NAME")
        [ -n "$WHO" ] || missing+=("WHO")
        if [ ${#missing[@]} -ne 0 ]; then
            zhh_section "Run Configuration"
            zhh_error "Missing required environment variables: ${missing[*]}"
            zhh_warn "Set them in .ka or your shell and run again."
            return 1
        fi
    elif [ -z "$VM_NAME" ]; then
        zhh_error "VM_NAME is not set. Please run \`source .ka\`."
        zhh_warn "Use \`zhh help\` for more info."
        return 1
    fi

    if ! $strict_run && [ -z "$TPU_TYPES" ]; then
        export TPU_TYPES="32,64"
    fi

    auto_select || return $?

    if [[ $VM_NAME =~ 'v4-' ]]; then
        export INF_ZONE=us-central2-b
    elif [[ $VM_NAME =~ 'v5litepod-' ]]; then
        export INF_ZONE=us-central1-a
    elif [[ $VM_NAME =~ 'v5p-' ]]; then
        # export INF_ZONE=us-east5-a
        zhh_debug "Current logic will not infer v5p zone automatically."
    elif [[ $VM_NAME =~ 'v6e-' ]]; then
        # export INF_ZONE=us-east1-d
        zhh_debug "Current logic will not infer v6e zone automatically."
    fi

    if [ -z "$ZONE" ]; then
        if [[ -z "$INF_ZONE" ]]; then
            zhh_error "Cannot infer ZONE from VM_NAME. Please set ZONE manually in .ka."
            return 1
        fi
        ZONE=$INF_ZONE
        export ZONE
        zhh_info "Inferred ZONE=$ZONE from VM_NAME=$VM_NAME."
    else
        if [[ ! -z "$INF_ZONE" && "$ZONE" != "$INF_ZONE" ]]; then
            # use red
            read -p $'\033[31m[Warning] ZONE='$ZONE' does not match the inferred zone '$INF_ZONE' from VM_NAME='$VM_NAME'. Continue? (y/N) \033[0m' yn
            if [ "$yn" != "y" ]; then
                echo "[INFO] Aborted."
                return 1
            fi
        fi
    fi

    if $strict_run; then
        zhh_section "Run Configuration"
        zhh_kv "PROJECT" "$PROJECT"
        zhh_kv "WANDB_API_KEY" "$(zhh_mask_secret "$WANDB_API_KEY") (masked)"
        zhh_kv "TPU_TYPES" "$TPU_TYPES"
        zhh_kv "VM_NAME" "$VM_NAME"
        zhh_kv "WHO" "$WHO"
        zhh_kv "ZONE" "${ZONE:-<empty>}"
    else
        zhh_success "Using VM_NAME=$VM_NAME (ZONE=$ZONE)"
    fi
}

# infer_stagedir has problem, since the command may be overwrite
# infer_stagedir(){
#     if [ -z "$VM_NAME" ]; then
#         echo -e "\033[31m[Internal Error] VM_NAME is not set. Contact admin.\033[0m" >&2
#         return 1
#     fi

#     LAST_COMMAND=$(get_command)

#     # parse: --workdir=(.*)/logs/\w+\s
#     STAGE_DIR=$(echo "$LAST_COMMAND" | grep -oP -- '--workdir=\K.*/logs/\w+\s' | sed 's|/logs/\w\+\s||')
#     # check STAGE_DIR exists
#     if [ ! -d "$STAGE_DIR" ]; then
#         echo -e "\033[31m[Error] Failed to infer staging directory\033[0m" >&2
#         return 1
#     fi

#     echo "$STAGE_DIR"
# }
