source scripts/apply.sh

ckpt_to_gs(){
    path=$1
    # path: /kmh-nfs-us-mount/staging/siri/unknown/launch_20250910_025630_git_5351ee2c/logs/log2_20250910_030359_39a8088a
    # output: gs://kmh-gcp-us-central2/qiao_zhicheng_hanhong_files/unknown/launch_20250910_025630_git_5351ee2c/logs/log2_20250910_030359_39a8088a
    subpath=$(echo $path | sed 's|/kmh-nfs-us-mount/staging/siri/||')
    output=gs://kmh-gcp-us-central2/qiao_zhicheng_hanhong_files/$subpath
    echo $output
}

stage(){
    if [ -z "$PROJECT" ]; then
        echo "Error: PROJECT is not set. Default to unknown"
        PROJECT=unknown
        return 1
    fi

    STAGE_ROOT=/kmh-nfs-us-mount/staging/siri/$PROJECT
    NOW_STR=$(date +'%Y%m%d_%H%M%S')
    RND_STR=$(cat /dev/urandom | tr -cd 'a-f0-9' | head -c 8)
    GIT_STR=$(git rev-parse --short HEAD)
    STAGE_DIR=$STAGE_ROOT/launch_${NOW_STR}_git${GIT_STR}_${RND_STR}

    echo staging to $STAGE_DIR

    sudo mkdir -p $STAGE_DIR
    sudo chmod 777 $STAGE_DIR
    echo staging files
    sudo rsync -a -O --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' --exclude 'logs' . $STAGE_DIR
}

zkill(){
    kill_tpu $VM_NAME $ZONE
}


run_job(){
    # args: $1=STAGE_DIR, $2...=extra args (passed to main.py)
    HERE=$PWD

    # VM_NAME must exists
    if [ -z "$VM_NAME" ]; then
        echo "Error: VM_NAME is not set."
        return 1
    fi

    STAGE_DIR=$1
    LOG_ROOT=$STAGE_DIR/logs

    NOW_STR=$(date +'%Y%m%d_%H%M%S')
    RND_STR=$(cat /dev/urandom | tr -cd 'a-f0-9' | head -c 8)
    exist_logs=$(ls $LOG_ROOT | wc -l)
    cur_log_id=$((exist_logs+1))
    LOG_DIR=$LOG_ROOT/log${cur_log_id}_${NOW_STR}_${RND_STR}

    # EXTRA_ARGS should be a list
    EXTRA_ARGS=()

    # check whether a checkpoint exists
    for check_id in $(seq $((cur_log_id-1)) -1 1); do
        LAST_LOG_DIR=$(ls $LOG_ROOT | grep log${check_id}_ | tail -n 1)
        
        # convert to gs bucket
        GS_DIR=$(ckpt_to_gs $LOG_ROOT/$LAST_LOG_DIR)
        echo "checking previous log dir $LOG_ROOT/$LAST_LOG_DIR -> $GS_DIR"
        # check if checkpoints exist in the gs bucket
        if gsutil ls $GS_DIR/checkpoint_* > /dev/null; then
            echo found previous checkpoint dir $LOG_ROOT/$LAST_LOG_DIR in gs $GS_DIR
            EXTRA_ARGS+=("--config.load_from=$LOG_ROOT/$LAST_LOG_DIR")
            break
        fi
        echo no checkpoints found in $LOG_ROOT/$LAST_LOG_DIR
    done;

    sudo mkdir -p $LOG_DIR
    sudo chmod 777 $LOG_DIR
    echo logging to $LOG_DIR

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
        echo "No extra args provided."
    else
        EXTRA_ARGS_STR=$(printf "'%s' " "${EXTRA_ARGS[@]}")
    fi

    COMMAND="python3 main.py --workdir=$LOG_DIR --mode=remote_run --config=configs/load_config.py:remote_run $EXTRA_ARGS_STR 2>&1 | tee $LOG_DIR/output.log"

    # register "$VM_NAME" -> "COMMAND" in /kmh-nfs-us-mount/staging/.sscript
    echo $COMMAND > /kmh-nfs-us-mount/staging/.sscript/$VM_NAME

    cd $STAGE_DIR && \
    gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE --worker=all --command "$DBG_COMMANDS && cd $STAGE_DIR && $COMMAND" && \
    cd $HERE
}

zrun(){
    # extra args are $@
    EXTRA_ARGS=("$@")

    # staging to $STAGE_DIR

    STAGE_DIR=$(stage)
    STAGE_DIR=$(echo $STAGE_DIR | head -n 1 | awk '{print $3}')
    echo "Staged to $STAGE_DIR"

    # if EXTRA_ARGS exists, write to a file in STAGE_DIR
    if [ -z "$EXTRA_ARGS" ]; then
        echo "No extra args provided."
    else
        printf "'%s' " "${EXTRA_ARGS[@]}" | sudo tee $STAGE_DIR/.extra_args
    fi

    # prepare TPU
    get_tpu $VM_NAME $ZONE && \
    setup_tpu $VM_NAME $ZONE && \
    run_job $STAGE_DIR "${EXTRA_ARGS[@]}"
}

zrerun(){
    # check if .extra_args exists
    EXTRA_ARGS=""
    if [ -f .extra_args ]; then
        EXTRA_ARGS=$(cat .extra_args)
        echo "Using extra args: $EXTRA_ARGS"
    else
        echo "No extra args file found."
    fi
    
    # prepare TPU
    get_tpu $VM_NAME $ZONE && \
    setup_tpu $VM_NAME $ZONE && \
    run_job $(pwd) "$EXTRA_ARGS"
}