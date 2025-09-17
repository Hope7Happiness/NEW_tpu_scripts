source $ZHH_SCRIPT_ROOT/scripts/common.sh
source $ZHH_SCRIPT_ROOT/scripts/setup.sh

if [ "$DO_TPU_SETUP" = "1" ]; then
    echo -e "\033[33m[Env Hint] TPU setup will be performed.\033[0m"
else
    echo -e "\033[33m[Env Hint] TPU setup will be skipped.\033[0m"
fi



get_tpu(){
    VM_NAME=$1
    ZONE=$2
    
    echo "[INFO] requesting tpu vm $VM_NAME in $ZONE..."

    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi
    outer_loop=0
    try_start=$(date)
    while true; do
        status=$(
            gcloud compute tpus tpu-vm describe $VM_NAME --zone=$ZONE --format="value(state)"
        )
        if [ "$status" = "READY" ]; then
            echo -e "\033[32m[INFO] TPU VM is ready.\033[0m"
            break
        elif [ -z "$status" ]; then
            echo "[INFO] TPU VM does not exist."
        elif [ "$status" = "PREEMPTED" ]; then
            echo "[INFO] TPU VM is preempted. Deleting..."
            gcloud compute tpus tpu-vm delete $VM_NAME --zone=$ZONE --quiet
        else
            echo "[INFO] TPU VM status: $status. Waiting..."
            continue
        fi
        success=0
        for i in {1..3}; do
            echo "[INFO] Creating TPU VM... Round $outer_loop Attempt $i (time: " $(date) ")"
            if gcloud compute tpus tpu-vm create $VM_NAME --zone=$ZONE --accelerator-type=v4-32 --version=tpu-ubuntu2204-base --spot --quiet 2>/dev/null ; then
                echo -e "\033[32m[INFO] TPU VM created successfully.\033[0m"
                success=1
                break
            fi
            echo "[INFO] Failed to create TPU VM. Retrying in 10 seconds..."
            sleep 10 # Wait for 10 seconds before retrying
        done
        if [ $success -eq 1 ]; then
            echo -e "\033[32m[INFO] TPU VM $VM_NAME created successfully.\033[0m"
            # if available, send email
            semail --apply-success $VM_NAME "$try_start" "$(date)" $outer_loop
            # for this case, TPU must be set up
            export DO_TPU_SETUP=1
            return
        fi
        sleep 60 # Wait for 1 minutes before checking again
        outer_loop=$((outer_loop+1))
        # if outer_loop % 100 == 0, send email
        if [ $((outer_loop % 100)) -eq 0 ]; then
            semail --apply-fail $VM_NAME "$try_start" "$(date)" $outer_loop
        fi
    done;
}

has_tpu(){
    VM_NAME=$1
    ZONE=$2

    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    status=$(
        gcloud compute tpus tpu-vm describe $VM_NAME --zone=$ZONE --format="value(state)" 2>/dev/null
    )
    if [ "$status" = "READY" ]; then
        return 0
    else
        return 1
    fi
}

setup_tpu(){
    VM_NAME=$1
    ZONE=$2

    echo "[INFO] setting up tpu vm $VM_NAME in $ZONE..."

    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    if [ "$DO_TPU_SETUP" = "1" ]; then
        mount_disk $VM_NAME $ZONE && \
        setup_env $VM_NAME $ZONE
    else
        echo "[INFO] Skipping TPU environment setup as DO_TPU_SETUP is not set."
        check_env $VM_NAME $ZONE
    fi

    wandb_login $VM_NAME $ZONE # enforce wandb login for each run
}

kill_tpu(){
    VM_NAME=$1
    ZONE=$2

    echo -e "\033[1m[INFO] killing tpu vm $VM_NAME in $ZONE...\033[0m"

    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    sleep 2
    gcloud compute tpus tpu-vm ssh $VM_NAME --zone=$ZONE --worker=all --command "
    ps -ef | grep main.py | grep -v grep | awk '{print \"kill -9 \" \$2}' | sort | uniq
    ps -ef | grep main.py | grep -v grep | awk '{print \"kill -9 \" \$2}' | sh
    sudo lsof -w /dev/accel0 | grep 'python' | grep -v 'grep' | awk '{print \"kill -9 \" \$2}' | sort | uniq
    sudo lsof -w /dev/accel0 | grep 'python' | grep -v 'grep' | awk '{print \"kill -9 \" \$2}' | sh
    echo job killed
    "
}
# This haven't been used
# check_and_kill(){
#     VM_NAME=$1
#     ZONE=$2

#     if [ -z "$VM_NAME" ]; then
#         echo -e $VM_UNFOUND_ERROR
#         return 1
#     fi

#     check_env $VM_NAME $ZONE || kill_tpu $VM_NAME $ZONE
# }