source $ZHH_SCRIPT_ROOT/scripts/common.sh
source $ZHH_SCRIPT_ROOT/scripts/setup.sh

# if [ "$DO_TPU_SETUP" = "1" ]; then
#     echo -e "\033[33m[Env Hint] TPU setup will be performed.\033[0m"
# else
#     echo -e "\033[33m[Env Hint] TPU setup will be skipped.\033[0m"
# fi

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

get_accelerator_version(){
    VM_NAME=$1

    # if card is *v6*
    if [[ $VM_NAME =~ v6 ]]; then
        echo "v2-alpha-tpuv6e"
    elif [[ $VM_NAME =~ v5e ]]; then
        echo "v2-alpha-tpuv5-lite"
    elif [[ $VM_NAME =~ v5p ]]; then
        echo "v2-alpha-tpuv5"
    else
        echo "tpu-ubuntu2204-base"
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
    accelerator_version=$(get_accelerator_version $VM_NAME)
    if [ $? -ne 0 ]; then
        return 1
    fi
    create_cmd="gcloud compute tpus tpu-vm create $VM_NAME --zone=$ZONE --accelerator-type=$accelerator_type --version=$accelerator_version --spot"

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
            sleep 60 # Wait for 1 minutes before checking again
            continue
        fi
        success=0
        for i in {1..3}; do
            echo "[INFO] Creating TPU VM... Round $outer_loop Attempt $i (time: " $(date) ")"
            if eval $create_cmd ; then
                echo -e "\033[32m[INFO] TPU VM created successfully.\033[0m"
                success=1
                break
            fi

            # all other calls are quiet
            if [ $i -eq 1 ] && [ $outer_loop -eq 0 ]; then create_cmd="$create_cmd --quiet 2>/dev/null"; fi

            echo "[INFO] Failed to create TPU VM. Retrying in 10 seconds..."
            sleep 10 # Wait for 10 seconds before retrying
        done
        if [ $success -eq 1 ]; then
            echo -e "\033[32m[INFO] TPU VM $VM_NAME created successfully.\033[0m"
            # if available, send email
            semail --apply-success $VM_NAME "$try_start" "$(date)" $outer_loop
            # for this case, TPU must be set up
            # export DO_TPU_SETUP=1
            export TPU_IS_NEW=1
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
    elif [ -z "$status" ]; then
        log_tpu_check_result deleted
        return 1
    else
        return 1
    fi
}

is_preempted(){
    VM_NAME=$1
    ZONE=$2

    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    status=$(
        gcloud compute tpus tpu-vm describe $VM_NAME --zone=$ZONE --format="value(state)" 2>/dev/null
    )
    # if in PREEMPTED or DELETED state, return true
    if [ "$status" = "PREEMPTED" ] || [ "$status" = "DELETED" ] || [ -z "$status" ]; then
        deregister_tpu $VM_NAME
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

good_tpu_verbose(){
    VM_NAME=$1
    ZONE=$2

    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    if ! has_tpu $VM_NAME $ZONE; then
        echo -e "\033[31m[Bad] TPU VM $VM_NAME is deleted.\033[0m"
        return
    fi

    if ! tpu_in_use $VM_NAME $ZONE; then
        echo -e "\033[33m[Info] TPU VM $VM_NAME is in use.\033[0m"
        return
    fi

    if check_env $VM_NAME $ZONE; then
        echo -e "\033[32m[Info] TPU VM $VM_NAME is ready.\033[0m"
    else
        echo -e "\033[31m[Internal Error] TPU VM $VM_NAME is not properly set up. Please use \`SCRIPT_DEBUG=1\` for more info.\033[0m"
    fi
}

setup_tpu(){
    # ZHH: change setup_tpu to must use setup
    if [ "$TPU_IS_NEW" = "1" ]; then
        echo "[INFO] TPU is newly created, running setup script."
        run_setup_script $VM_NAME $ZONE
    else
        echo "[INFO] TPU is existing, first skip setup script."
    fi
    while_check_env $VM_NAME $ZONE && ret=0 || ret=$?
    if [ $ret -eq 9 ]; then
        echo "[INFO] TPU may be preempted during environment check. Exiting to re-apply..."
        return 9
    elif [ $ret -ne 0 ]; then
        echo "[INFO] Environment check failed."
        return $ret
    fi
    run_wandb_login $VM_NAME $ZONE
}

get_and_setup_tpu(){
    ret=9
    trial=0
    while [ $ret -eq 9 ]; do
        echo "[INFO] Attempt number $((trial+1)) to get and setup TPU..."
        get_tpu $VM_NAME $ZONE
        setup_tpu $VM_NAME $ZONE && ret=0 || ret=$?
        if [ $ret -eq 0 ]; then
            echo -e "\033[32m[INFO] TPU $VM_NAME @ $ZONE is ready to use.\033[0m"
            return 0
        fi
        trial=$((trial+1))
        if [ $trial -ge 5 ]; then
            echo -e "\033[31m[Error] TPU $VM_NAME @ $ZONE setup failed after 5 trials. Exiting.\033[0m"
            return 1
        fi
        sleep 300
    done
    echo -e "\033[31m[ERROR] get_and_setup_tpu exited with ret=$ret\033[0m"
    return $ret
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
