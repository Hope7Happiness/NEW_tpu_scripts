source $ZHH_SCRIPT_ROOT/scripts/common.sh
source $ZHH_SCRIPT_ROOT/scripts/setup.sh

if [ "$DO_TPU_SETUP" = "1" ]; then
    echo -e "\033[33m[Env Hint] TPU setup will be performed.\033[0m"
else
    echo -e "\033[33m[Env Hint] TPU setup will be skipped.\033[0m"
fi

get_accelerator_args(){
    VM_NAME=$1

    # if match kmh-tpuvm-(v\d-\d+) in name
    if [[ $VM_NAME =~ kmh-tpuvm-(v[a-zA-Z0-9]+-[0-9]+) ]]; then
        echo "${BASH_REMATCH[1]}"
    else
        echo -e "\033[31m[Internal Error] Cannot parse accelerator type from VM name $VM_NAME. Contact admin.\033[0m"
        return 1
    fi
}

get_tpu(){
    VM_NAME=$1
    ZONE=$2
    
    echo "[INFO] requesting tpu vm $VM_NAME in $ZONE..."

    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    # get accelerator args
    accelerator_type=$(get_accelerator_args $VM_NAME)
    if [ $? -ne 0 ]; then
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

good_tpu(){
    VM_NAME=$1
    ZONE=$2

    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    if ! has_tpu $VM_NAME $ZONE; then
        return 2
    fi
    check_env $VM_NAME $ZONE || return $?
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
    fi
    # check_env $VM_NAME $ZONE && \
    while_check_env $VM_NAME $ZONE && \
    wandb_login $VM_NAME $ZONE # enforce wandb login for each run
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