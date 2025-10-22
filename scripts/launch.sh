source $ZHH_SCRIPT_ROOT/scripts/apply.sh
source $ZHH_SCRIPT_ROOT/scripts/sscript.sh

ckpt_to_gs(){
    path=$1
    # path: /kmh-nfs-us-mount/staging/siri/PROJECT/other_parts
    # output: gs://kmh-gcp-us-central2/qiao_zhicheng_hanhong_files/PROJECT/other_parts
    subpath=$(echo $path | sed 's|/kmh-nfs-ssd-us-mount/staging/siri/||')
    output=gs://kmh-gcp-us-central2/qiao_zhicheng_hanhong_files/$subpath
    echo $output
}

stage(){
    # DO NOT modify the output format, it is parsed in zrun
    if [ -z "$PROJECT" ]; then
        echo -e "\033[31m[Warning]: PROJECT is not set. Default to 'unknown'\033[0m" >&2
        PROJECT=unknown
    fi

    STAGE_ROOT=/kmh-nfs-ssd-us-mount/staging/siri/$PROJECT
    NOW_STR=$(date +'%Y%m%d_%H%M%S')
    RND_STR=$(cat /dev/urandom | tr -cd 'a-f0-9' | head -c 8)
    GIT_STR=$(git rev-parse --short HEAD)
    STAGE_DIR=$STAGE_ROOT/launch_${NOW_STR}_git${GIT_STR}_${RND_STR}

    echo staging to $STAGE_DIR

    sudo mkdir -p $STAGE_DIR
    sudo chmod 777 $STAGE_DIR
    echo "[INFO] staging files" >&2
    sudo rsync -a -O --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' --exclude 'logs' . $STAGE_DIR
}

zkill(){
    kill_tpu $VM_NAME $ZONE
    fail_command
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
    LOG_DIR=$LOG_ROOT/log${cur_log_id}_${NOW_STR}_VM${VM_NAME}_Z${ZONE}_${RND_STR}

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

    DBG_COMMANDS="ls $CONDA_PY_PATH"
    py_path=$CONDA_PY_PATH
    # if VM_NAME contains v6, don't use conda
    if use_v6_script $VM_NAME; then
        py_path="python"
        DBG_COMMANDS="which python"
    fi

    # if EXTRA_ARGS exists:
    if [ -z "$EXTRA_ARGS" ]; then
        echo "[INFO] No extra args provided."
    else
        EXTRA_ARGS_STR=$(printf "'%s' " "${EXTRA_ARGS[@]}")
    fi

    COMMAND="$py_path main.py --workdir=$LOG_DIR --mode=remote_run --config=configs/load_config.py:remote_run $EXTRA_ARGS_STR 2>&1"
    # COMMAND="ls /foo/bar | sudo tee -a $LOG_DIR/output.log"

    # register command
    log_command "$COMMAND"
    echo "[INFO] running command: $COMMAND"
    (echo "$COMMAND"; echo ========; echo; ) > $LOG_DIR/output.log

    cd $STAGE_DIR && \
    gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE --worker=all --command "$DBG_COMMANDS && cd $STAGE_DIR && $COMMAND" 2>&1 | stdbuf -oL -eL sudo tee -a $LOG_DIR/output.log

    status=${PIPESTATUS[0]}   # this get the return code of gcloud
    [ $status -eq 0 ] || (
        echo -e "\033[31m[Error] Job failed. Check logs in $LOG_DIR/output.log\033[0m" >&2
        fail_command
        # NOTE: default, we don't release queue slot on failure
        return 7
    ) && (
        echo -e "\033[32m[Success] Job finished (maybe failed). Check logs in $LOG_DIR/output.log\033[0m" >&2
        success_command
        cd $HERE

        # release a queue slot
        echo "[INFO] Releasing a queue slot..."
        release_queue
    )
}

while_run(){
    STAGE_DIR=$1
    # extra args are $2...
    EXTRA_ARGS=("${@:2}")

    run_job $STAGE_DIR "${EXTRA_ARGS[@]}" && ret=0 || ret=$?

    # if ret==7 (job failed), auto-check card status, if bad, re-setup env and re-run
    while [ $ret -eq 7 ]; do
        echo -e "\033[31m[Error] Job failed, auto-checking card status...\033[0m"
        if ! is_preempted $VM_NAME $ZONE; then
            # note: better make the code more likely to enter this branch
            # this will avoid infinite loop

            # check if there is GRPC error
            if grep -q "This TPU is going through a maintenance event, and might be unavailable" $LOG_DIR/output.log; then
                echo -e "\033[33m[Info] Found maintenance event in logs, this TPU is no longer usable. Aborted.\033[0m"
                return 1
            elif grep -q "Failed to execute command on multiple workers. This may have happened if you have not added your SSH key to your ssh-agent" $LOG_DIR/output.log || grep -q "Terminating process because the coordinator detected missing heartbeats." $LOG_DIR/output.log; then
                echo -e "\033[33m[Info] Found GRPC/heartbeat error in logs, will re-setup env and re-run.\033[0m"
                # sleep 60
                # kill_tpu $VM_NAME $ZONE
                sleep 60
                setup_tpu $VM_NAME $ZONE && \
                kill_tpu $VM_NAME $ZONE && \
                run_job $STAGE_DIR "${EXTRA_ARGS[@]}" && ret=0 || ret=$?
            else
                echo -e "\033[32m[Info] Card status looks good, then it is probably a code bug. Please fix it and re-run.\033[0m"
                return 1
            fi
        else
            echo -e "\033[33m[Info] Card is PREEMPTED, will re-apply and re-run.\033[0m"
            get_tpu $VM_NAME $ZONE && \
            setup_tpu $VM_NAME $ZONE && \
            run_job $STAGE_DIR "${EXTRA_ARGS[@]}" && ret=0 || ret=$?
        fi
    done
}

zget(){
    # get tpu only
    get_tpu $VM_NAME $ZONE && \
    setup_tpu $VM_NAME $ZONE && \
    register_tpu
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
    register_tpu && \
    while_run $STAGE_DIR "${EXTRA_ARGS[@]}"
}

zrerun(){
    # check if in staging dir
    if [[ ! $(pwd) =~ /kmh-nfs-ssd-us-mount/staging/ ]]; then
        echo -e "\033[31m[Error] You are NOT in a staging directory. Aborted.\033[0m" >&2
        return 1
    fi

    # check if .extra_args exists
    # EXTRA_ARGS=""
    if [ -f .extra_args ]; then
        read -a EXTRA_ARGS < <(cat .extra_args) || true
        echo "[INFO] Using extra args: ${EXTRA_ARGS[@]}"
    else
        echo "[INFO] No extra args found."
    fi
    
    # prepare TPU
    get_tpu $VM_NAME $ZONE && \
    setup_tpu $VM_NAME $ZONE && \
    register_tpu && \
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
    STAGE_DIR=$(stage)
    STAGE_DIR=$(echo $STAGE_DIR | head -n 1 | awk '{print $3}')

    # if EXTRA_ARGS exists, write to a file in STAGE_DIR
    if [ -z "$EXTRA_ARGS" ]; then
        echo "[INFO] No extra args provided."
    else
        printf "'%s' " "${EXTRA_ARGS[@]}" | sudo tee $STAGE_DIR/.extra_args
    fi

    if good_tpu $VM_NAME $ZONE && queue_isempty $VM_NAME && ! has_failure $VM_NAME; then
        echo -e "\033[32m[INFO] TPU VM $VM_NAME is already free and no jobs in queue. Directly running...\033[0m"
        setup_tpu $VM_NAME $ZONE && \
        while_run $STAGE_DIR "${EXTRA_ARGS[@]}" && ret=0 || ret=$?
        return $ret
    fi

    queue_job $STAGE_DIR && \
    setup_tpu $VM_NAME $ZONE && \
    register_tpu && \
    while_run $STAGE_DIR "${EXTRA_ARGS[@]}"
}

zqueue_pop(){
    # release a queue slot
    get_tpu $VM_NAME $ZONE && \
    setup_tpu $VM_NAME $ZONE && \
    register_tpu && \
    release_queue
}

run_matmul(){
    # use matmul to keep a TPU busy
    get_tpu $VM_NAME $ZONE && \
    setup_tpu $VM_NAME $ZONE

    DBG_COMMANDS="ls $CONDA_PY_PATH"
    py_path=$CONDA_PY_PATH
    # if VM_NAME contains v6, don't use conda
    if use_v6_script $VM_NAME; then
        py_path="python"
        DBG_COMMANDS="which python"
    fi

    MATMUL_SCRIPT="import jax as j,time as t;from flax.jax_utils import replicate as e;p=j.numpy;r=j.random;k=r.PRNGKey(0);N=1<<15;_T=e(r.normal(k,(N,N)));__=j.pmap(lambda _: _.T@_/p.linalg.norm(_@_.T));exec('while True: (__(_T), t.sleep(0.5))')"


    COMMAND="$py_path -c \"$MATMUL_SCRIPT\" 2>&1"
    log_command "$COMMAND"
    # echo "This is going to stuck. Use this to kill: " "gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE --worker=all --command=\"ps -ef | grep python | grep linalg | grep -v grep | awk '{print \\\"kill -9 \\\" \\\$2}' | sh\""
    gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE --worker=all --command "$DBG_COMMANDS && $COMMAND" # never ends
}

zstatus(){
    # the original "zzz"
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
        if ! get_tpu_check_result $VM_NAME | grep -q "deleted"; then
            echo -e "\033[33m[Info] TPU $VM_NAME is not deleted, skip deregister.\033[0m"
            continue
        fi
        deregister_tpu $VM_NAME
        echo -e "\033[32m[Info] Deregistered TPU $VM_NAME\033[0m"
    done;
}

check_config_sanity(){
    if [ -z "$VM_NAME" ]; then
        echo -e "\033[31m[Error] VM_NAME is not set. Please run \`source ka.sh\`.\033[0m" >&2
        echo -e "\033[33m[Hint] Use \`zhh help\` for more info.\033[0m" >&2
        return 1
    fi

    if [[ $VM_NAME =~ v4 ]]; then
        export INF_ZONE=us-central2-b
    elif [[ $VM_NAME =~ v5litepod ]]; then
        export INF_ZONE=us-central1-a
    elif [[ $VM_NAME =~ v5p ]]; then
        export INF_ZONE=us-east5-a
    elif [[ $VM_NAME =~ v6e ]]; then
        # export INF_ZONE=us-east1-d
        echo "current will not infer v6e zone"
    fi

    if [ -z "$ZONE" ]; then
        echo -e "\033[33m[Info] ZONE is not set. Will automatically infer zone.\033[0m" >&2

        if [[ -z "$INF_ZONE" ]]; then
            echo -e "\033[31m[Error] Cannot infer ZONE from VM_NAME. Please set ZONE manually in ka.sh.\033[0m" >&2
            return 1
        fi
        ZONE=$INF_ZONE
        echo -e "\033[32m[Info] Inferred ZONE=$ZONE from VM_NAME=$VM_NAME.\033[0m"
        sleep 2
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