source $ZHH_SCRIPT_ROOT/scripts/common.sh

wrap_gcloud(){
    if [ ! -z "$SCRIPT_DEBUG" ]; then
        gcloud "$@"
    else
        gcloud "$@" > /dev/null 2>&1
    fi
}

use_v6_script(){
    # use_v6_script means:
    # 1. use gs bucket for python env
    VM_NAME=$1

    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    if [[ $VM_NAME =~ v6e ]]; then
        return 0
    elif [[ $VM_NAME =~ v5p ]]; then
        return 0
    else
        return 1
    fi
}

use_v5_env(){
    # use v5 env instead of v6 during installation
    VM_NAME=$1

    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    # if [[ $VM_NAME =~ v6e ]]; then
        # return 0
    if [[ $VM_NAME =~ v5p ]]; then
        return 0
    else
        return 1
    fi
}

zone_to_gs(){
    ZONE=$1

    if [ -z "$ZONE" ]; then
        echo -e $ZONE_UNFOUND_ERROR
        return 1
    fi

    if [[ $ZONE =~ us-central2.* ]]; then
        echo "gs://kmh-gcp-us-central2"
    elif [[ $ZONE =~ us-east1.* ]]; then
        echo "gs://kmh-gcp-us-east1"
    elif [[ $ZONE =~ us-east5.* ]]; then
        # ZHH: now we have bucket for east5!
        echo "gs://kmh-gcp-us-east5"
    elif [[ $ZONE =~ us-central1.* ]]; then
	    echo "gs://kmh-gcp-us-central1"
    elif [[ $ZONE =~ asia-northeast1.* ]]; then
        echo "gs://kmh-gcp-asia-northeast1-b" # special case, by zy
    else
        echo -e $ZONE_UNFOUND_ERROR >&2
        exit 1
    fi
}

check_env(){
    # Check whether JAX can run

    VM_NAME=$1
    ZONE=$2

    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    py_path=$CONDA_PY_PATH
    # if VM_NAME contains v6, don't use conda
    IS_V6=0
    if use_v6_script $VM_NAME; then
        py_path="python"
        IS_V6=1
    fi

    ENV_CHECK="$py_path -c 'import jax, torch; print(jax.__file__)'"
    # read both stdout and stderr
    result=$(timeout 60s gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE \
    --worker=all --command "$ENV_CHECK" 2>&1 || true)
    # first, eliminate module not found
    if [[ $result == *"ModuleNotFoundError"* ]]; then
        echo "Environment is not proper setup: Cannot find torch/jax. Use \`SCRIPT_DEBUG=1\` for more info."
        return 4
    fi
    # if not IS_V6, assert miniforge3 is in result
    if [ ! $IS_V6 -eq 1 ]; then
        if [[ $result =~ *"local"* ]]; then
            echo "Wrong python env, expected to in miniforge3. Gonna remove local..."
            wrap_gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE \
            --worker=all --command "sudo rm -rf ~/.local"
        fi
    fi

    TEST="$py_path -c 'import jax; print(jax.devices())'"
    # read both stdout and stderr
    result=$(timeout 120s gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE \
    --worker=all --command "$TEST" 2>&1 || true)

    if [[ $result == *"TpuDevice"* ]]; then
        echo "Environment setup successful."
    elif [[ $result == *"jaxlib.xla_extension.XlaRuntimeError: ABORTED: The TPU is already in use by process with pid"* ]]; then
        echo "TPU is already in use. If you want to persist, use \`zhh k\` and try again."
        return 3
    elif [[ $result == *"[/usr/bin/ssh] exited with return code [255]"* ]]; then
        echo "TPU may be preempted. Gonna re-apply..."
        return 9
    else
        echo "TPU Unkwown Error"
        echo "$result"
        return 4
    fi
}

while_check_env(){
    # allow user to run "kill" to interrupt
    VM_NAME=$1
    ZONE=$2

    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    echo "[INFO] Checking environment setup..."
    check_env $VM_NAME $ZONE && ret=0 || ret=$?
    if [ $ret -eq 0 ]; then
        echo "[INFO] Environment is ready."
        return 0
    fi
    if [ $ret -eq 3 ]; then
        if [ "$ZAK" = "1" ]; then
            echo "[INFO] Auto-kill is enabled. Attempting to kill the TPU process..."
            yn="y"
        else
            read -p "Kill the TPU process right now? (y/n) " yn
        fi
        if [ "$yn" = "y" ]; then
            kill_tpu $VM_NAME $ZONE || true
        else
            echo "[INFO] Not killing the TPU process. Exiting."
            return 3
        fi
    elif [ $ret -eq 4 ]; then
        echo "[INFO] Environment check failed. Retrying verbose setup..."
        export SCRIPT_DEBUG=1
        export ZAK=1 # sometimes need to autokill even in the setup phase
        run_setup_script $VM_NAME $ZONE || true
    elif [ $ret -eq 9 ]; then
        echo "[INFO] TPU may be preempted. Exiting to re-apply..."
        return 9
    fi
    check_env $VM_NAME $ZONE && ret=0 || ret=$?
    if [ $ret -ne 0 ]; then
        echo -e "\033[31m[Error] Environment is not proper setup: failed to init TPU. Use \`SCRIPT_DEBUG=1\` for more info.\033[0m"
    fi
    return $ret
}

kill_tpu(){
    VM_NAME=$1
    ZONE=$2

    echo -e "\033[1m[INFO] killing tpu vm $VM_NAME in $ZONE...\033[0m"

    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    # KILLER=$(cat $ZHH_SCRIPT_ROOT/scripts/new_killer.sh)

    sleep 2
    gcloud compute tpus tpu-vm ssh $VM_NAME --zone=$ZONE --worker=all --command "
    sudo bash $ZHH_SCRIPT_ROOT/scripts/new_killer.sh
    echo job killed
    "
}


tpu_in_use(){
    # use the newest script
    IN_USE_SCRIPT="
    # if /dev/accel0 exist, check
    if [[ -e /dev/accel0 ]]; then
        ret=\$(sudo lsof -w /dev/accel0 | wc -l)
        if [[ \"\$ret\" -ne 0 ]]; then
            exit 1
        else
            exit 0
        fi
    fi

    if [[ -e /dev/vfio/0 ]]; then
        ret=\$(sudo lsof -w /dev/vfio/0 | wc -l)
        if [[ \"\$ret\" -ne 0 ]]; then
            exit 1
        else
            exit 0
        fi
    fi

    echo \"No TPU device found\"
    exit 2
    "
    wrap_gcloud compute tpus tpu-vm ssh $VM_NAME --zone=$ZONE --worker=all --command "$IN_USE_SCRIPT" && ret=0 || ret=$?
    return $ret
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
            gcloud auth activate-service-account --key-file=/kmh-nfs-ssd-us-mount/code/qiao/sqa-sa_do_not_deleet.json
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
            # gsutil -m cp -r $gs_str/hanhong/v6_wheels.tar.gz ./wheels.tar.gz
            # ### new update
            gcloud auth activate-service-account --key-file=/kmh-nfs-ssd-us-mount/code/qiao/sqa-sa_do_not_deleet.json
            gsutil -m cp -r $gs_str/hanhong/v6_wheels_jax437.tar.gz ./wheels.tar.gz
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
        # if [ "$DO_TPU_SETUP" != "1" ]; then
        #     echo -e "\033[33m[Hint] Is the TPU set up? Use \`DO_TPU_SETUP=1\` to force environment setup on TPU VM.\033[0m"
        # fi
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