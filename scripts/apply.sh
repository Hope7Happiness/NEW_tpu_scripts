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

run_setup_script(){
    VM_NAME=$1
    ZONE=$2

    echo "[INFO] setting up tpu vm $VM_NAME in $ZONE..."

    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    py_path=$CONDA_PY_PATH
    # if VM_NAME contains v6, don't use conda
    if use_v6_script $VM_NAME; then
        py_path="python"
    fi

    # if [ "$DO_TPU_SETUP" = "1" ]; then
    MOUNT_DISK_STR=$(cat $ZHH_SCRIPT_ROOT/scripts/mount_disk.sh)

    if use_v6_script $VM_NAME; then
        gs_str=$(zone_to_gs $ZONE)
        if use_v5_env $VM_NAME; then
            PIP_INSTALL_STR="
            set -euo pipefail

            cd
            gsutil -m cp -r $gs_str/hanhong/v5_wheels.tar.gz ./wheels.tar.gz
            tar -xvf wheels.tar.gz
            rm -rf .local || true
            pip install --no-index --find-links=wheels wheels/*.whl --no-deps --force-reinstall --no-warn-script-location
            rm -rf wheels wheels.tar.gz
            "
        else
            PIP_INSTALL_STR="
            set -euo pipefail

            cd
            gsutil -m cp -r $gs_str/hanhong/v6_wheels.tar.gz ./wheels.tar.gz
            tar -xvf wheels.tar.gz
            rm -rf .local || true
            pip install --no-index --find-links=wheels wheels/*.whl --no-deps --force-reinstall --no-warn-script-location
            rm -rf wheels wheels.tar.gz
            "
        fi
    else
        PIP_INSTALL_STR=$(cat $ZHH_SCRIPT_ROOT/scripts/install.sh)
    fi
    # else
        # echo "[INFO] Skipping TPU environment setup as DO_TPU_SETUP is not set."
    # fi

    CMD="
    $MOUNT_DISK_STR
    $PIP_INSTALL_STR
    "

    wrap_gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE \
    --worker=all --command "$CMD" && ret=0 || ret=$?
    if [ $ret -ne 0 ]; then
        echo -e "\033[31m[Error] Environment setup failed. Use \`SCRIPT_DEBUG=1\` for more info.\033[0m"
        # check if DO_TPU_SETUP is not set
        if [ "$DO_TPU_SETUP" != "1" ]; then
            echo -e "\033[33m[Hint] Is the TPU set up? Use \`DO_TPU_SETUP=1\` to force environment setup on TPU VM.\033[0m"
        fi
        return 1
    fi
}

run_wandb_login(){
    VM_NAME=$1
    ZONE=$2

    echo "[INFO] wandb login into $VM_NAME in $ZONE..."

    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    if [ -z "$WANDB_API_KEY" ]; then
        echo -e "\033[31m[Error] WANDB_API_KEY is not set, so you cannot perform wandb login. Please run \`source ka.sh\`.\033[0m" >&2
        return 1
    fi

    py_path=$CONDA_PY_PATH
    # if VM_NAME contains v6, don't use conda
    if use_v6_script $VM_NAME; then
        py_path="python"
    fi
    WANDB_LOGIN_STR="$py_path -m wandb login $WANDB_API_KEY"
    # else
        # echo "[INFO] Skipping TPU environment setup as DO_TPU_SETUP is not set."
    # fi

    CMD="
    $WANDB_LOGIN_STR
    "

    wrap_gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE \
    --worker=all --command "$CMD" && ret=0 || ret=$?
    if [ $ret -ne 0 ]; then
        echo -e "\033[31m[Error] Wandb login failed. Contact ZHH or use \`SCRIPT_DEBUG=1\` for more info.\033[0m"
        return 1
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
    while_check_env $VM_NAME $ZONE
    run_wandb_login $VM_NAME $ZONE
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
