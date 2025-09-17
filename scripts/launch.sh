source $ZHH_SCRIPT_ROOT/scripts/apply.sh
source $ZHH_SCRIPT_ROOT/scripts/sscript.sh

ckpt_to_gs(){
    path=$1
    # path: /kmh-nfs-us-mount/staging/siri/PROJECT/other_parts
    # output: gs://kmh-gcp-us-central2/qiao_zhicheng_hanhong_files/PROJECT/other_parts
    subpath=$(echo $path | sed 's|/kmh-nfs-us-mount/staging/siri/||')
    output=gs://kmh-gcp-us-central2/qiao_zhicheng_hanhong_files/$subpath
    echo $output
}

stage(){
    # DO NOT modify the output format, it is parsed in zrun
    if [ -z "$PROJECT" ]; then
        echo -e "\033[31m[Warning]: PROJECT is not set. Default to 'unknown'\033[0m" >&2
        PROJECT=unknown
    fi

    STAGE_ROOT=/kmh-nfs-us-mount/staging/siri/$PROJECT
    NOW_STR=$(date +'%Y%m%d_%H%M%S')
    RND_STR=$(cat /dev/urandom | tr -cd 'a-f0-9' | head -c 8)
    GIT_STR=$(git rev-parse --short HEAD)
    STAGE_DIR=$STAGE_ROOT/launch_${NOW_STR}_git${GIT_STR}_${RND_STR}

    echo staging to $STAGE_DIR

    sudo mkdir -p $STAGE_DIR
    sudo chmod 777 $STAGE_DIR
    echo "[INFO] staging files"
    sudo rsync -a -O --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' --exclude 'logs' . $STAGE_DIR
}

zkill(){
    kill_tpu $VM_NAME $ZONE
}


run_job(){
    # args: $1=STAGE_DIR, $2...=extra args (passed to main.py)
    HERE=$PWD

    STAGE_DIR=$1
    LOG_ROOT=$STAGE_DIR/logs

    NOW_STR=$(date +'%Y%m%d_%H%M%S')
    RND_STR=$(cat /dev/urandom | tr -cd 'a-f0-9' | head -c 8)
    exist_logs=$(ls $LOG_ROOT 2>/dev/null | wc -l)
    cur_log_id=$((exist_logs+1))
    LOG_DIR=$LOG_ROOT/log${cur_log_id}_${NOW_STR}_${RND_STR}

    # EXTRA_ARGS should be a list
    EXTRA_ARGS=()

    # check whether a checkpoint exists
    echo "[INFO] finding checkpoints..."
    for check_id in $(seq $((cur_log_id-1)) -1 1); do
        LAST_LOG_DIR=$(ls $LOG_ROOT | grep log${check_id}_ | tail -n 1)
        
        # convert to gs bucket
        GS_DIR=$(ckpt_to_gs $LOG_ROOT/$LAST_LOG_DIR)
        echo "[INFO] |___checking previous log dir $LOG_ROOT/$LAST_LOG_DIR -> $GS_DIR"
        # check if checkpoints exist in the gs bucket
        if gsutil ls $GS_DIR/checkpoint_* > /dev/null; then
            echo "[INFO]   ===>found previous checkpoint dir $LOG_ROOT/$LAST_LOG_DIR in gs $GS_DIR"
            EXTRA_ARGS+=("--config.load_from=$LOG_ROOT/$LAST_LOG_DIR")
            break
        fi
        echo "[INFO] no checkpoints found in $LOG_ROOT/$LAST_LOG_DIR"
    done;

    sudo mkdir -p $LOG_DIR
    sudo chmod 777 $LOG_DIR
    echo "[INFO] logging to $LOG_DIR"

    # extra args is $@[2:]
    # EXTRA_ARGS=("${EXTRA_ARGS[@]}" "${@:2}")
    for arg in "${@:2}"; do
        if [[ "$arg" =~ [[:alnum:]_] ]]; then # only add useful args
            EXTRA_ARGS+=("$arg")
        fi
    done

    DBG_COMMANDS="which python"

    # if EXTRA_ARGS exists:
    if [ -z "$EXTRA_ARGS" ]; then
        echo "[INFO] No extra args provided."
    else
        EXTRA_ARGS_STR=$(printf "'%s' " "${EXTRA_ARGS[@]}")
    fi

    # COMMAND="python3 main.py --workdir=$LOG_DIR --mode=remote_run --config=configs/load_config.py:remote_run $EXTRA_ARGS_STR 2>&1 | sudo tee -a $LOG_DIR/output.log"
    COMMAND="ls /foo/bar | sudo tee -a $LOG_DIR/output.log"

    # register command
    log_command "$COMMAND"
    echo "[INFO] running command: $COMMAND"
    (echo "$COMMAND"; echo ========; echo; ) > $LOG_DIR/output.log

    (
        cd $STAGE_DIR && \
        gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE --worker=all --command "$DBG_COMMANDS && cd $STAGE_DIR && $COMMAND"
    ) || (
        echo -e "\033[31m[Error] Job failed. Check logs in $LOG_DIR/output.log\033[0m" >&2
        fail_command
        # NOTE: default, we don't release queue slot on failure
        return 7
    ) && (
        echo -e "\033[32m[Success] Job finished. Check logs in $LOG_DIR/output.log\033[0m" >&2
        success_command
        cd $HERE

        # release a queue slot
        echo "[INFO] Releasing a queue slot..."
        release_queue
    )
}

zrun(){
    # extra args are $@
    EXTRA_ARGS=("$@")

    # staging to $STAGE_DIR

    STAGE_DIR=$(stage)
    STAGE_DIR=$(echo $STAGE_DIR | head -n 1 | awk '{print $3}')

    # if EXTRA_ARGS exists, write to a file in STAGE_DIR
    if [ -z "$EXTRA_ARGS" ]; then
        echo "[INFO] No extra args provided."
    else
        printf "'%s' " "${EXTRA_ARGS[@]}" | sudo tee $STAGE_DIR/.extra_args
    fi

    # prepare TPU
    get_tpu $VM_NAME $ZONE && \
    setup_tpu $VM_NAME $ZONE && \
    run_job $STAGE_DIR "${EXTRA_ARGS[@]}"
    ret=$?

    # if ret==7 (job failed), auto-check card status, if bad, re-setup env and re-run
    while [ $ret -eq 7 ]; do
        echo -e "\033[31m[Error] Job failed, auto-checking card status...\033[0m"
        sleep 60 # wait for a minute to let TPU recover
        if has_tpu $VM_NAME $ZONE; then
            echo -e "\033[32m[Info] Card status looks good, then it is probably a code bug. Please fix it and re-run.\033[0m"
            return 1
        else
            echo -e "\033[33m[Info] Card is PREEMPTED, will re-apply and re-run.\033[0m"
            get_tpu $VM_NAME $ZONE && \
            setup_tpu $VM_NAME $ZONE && \
            run_job $STAGE_DIR "${EXTRA_ARGS[@]}"
            ret=$?
        fi
    done
}

zrerun(){
    # check if in staging dir
    if [[ ! $(pwd) =~ /kmh-nfs-us-mount/staging/ ]]; then
        echo -e "\033[31m[Error] You are NOT in a staging directory. Aborted.\033[0m" >&2
        return 1
    fi

    # check if .extra_args exists
    EXTRA_ARGS=""
    if [ -f .extra_args ]; then
        EXTRA_ARGS=$(cat .extra_args)
        echo "[INFO] Using extra args: $EXTRA_ARGS"
    else
        echo "[INFO] No extra args found."
    fi
    
    # prepare TPU
    get_tpu $VM_NAME $ZONE && \
    setup_tpu $VM_NAME $ZONE && \
    run_job $(pwd) "$EXTRA_ARGS"
}

zqueue(){
    EXTRA_ARGS=("$@")

    # confirm
    read -p "Queue the job on $VM_NAME, with args $EXTRA_ARGS ? (y/N) " yn
    if [ "$yn" != "y" ]; then
        echo "[INFO] Aborted."
        return 1
    fi

    # staging to $STAGE_DIR
    STAGE_DIR=$(stage)
    STAGE_DIR=$(echo $STAGE_DIR | head -n 1 | awk '{print $3}')

    # if EXTRA_ARGS exists, write to a file in STAGE_DIR
    if [ -z "$EXTRA_ARGS" ]; then
        echo "[INFO] No extra args provided."
    else
        printf "'%s' " "${EXTRA_ARGS[@]}" | sudo tee $STAGE_DIR/.extra_args
    fi

    queue_job $STAGE_DIR && \
    setup_tpu $VM_NAME $ZONE && \
    run_job $STAGE_DIR "${EXTRA_ARGS[@]}"
}

zqueue_pop(){
    # release a queue slot
    release_queue
    echo "[INFO] Released a queue slot."
}

check_config_sanity(){
    if [ -z "$VM_NAME" ]; then
        echo -e "\033[31m[Error] VM_NAME is not set. Please run \`source ka.sh\`.\033[0m" >&2
        return 1
    fi

    if [ -z "$ZONE" ]; then
        echo -e "\033[31m[Error] ZONE is not set. Please run \`source ka.sh\`.\033[0m" >&2
        return 1
    fi

    if [ -z "$WANDB_API_KEY" ]; then
        echo -e "\033[31m[Error] WANDB_API_KEY is not set. Please run \`source ka.sh\`.\033[0m" >&2
        return 1
    fi

    echo -e "\033[32m[INFO] You are using VM_NAME=$VM_NAME (ZONE=$ZONE)\033[0m"
    sleep 2
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