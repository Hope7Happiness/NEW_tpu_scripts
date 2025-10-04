source $ZHH_SCRIPT_ROOT/scripts/common.sh

wrap_gcloud(){
    if [ ! -z "$SCRIPT_DEBUG" ]; then
        gcloud "$@"
    else
        gcloud "$@" > /dev/null 2>&1
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
    if [[ $VM_NAME =~ v6e ]]; then
        py_path="python"
        IS_V6=1
    fi

    ENV_CHECK="$py_path -c 'import jax, torch; print(jax.__file__)'"
    # read both stdout and stderr
    result=$(timeout 60s gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE \
    --worker=all --command "$ENV_CHECK" 2>&1 || true)
    # first, eliminate module not found
    if [[ $result == *"ModuleNotFoundError"* ]]; then
        echo "Environment setup failed. Cannot find torch/jax. Use \`SCRIPT_DEBUG=1\` for more info."
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
    result=$(timeout 90s gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE \
    --worker=all --command "$TEST" 2>&1 || true)

    if [[ $result == *"TpuDevice"* ]]; then
        echo "Environment setup successful."
    elif [[ $result == *"jaxlib.xla_extension.XlaRuntimeError: ABORTED: The TPU is already in use by process with pid"* ]]; then
        echo "TPU is already in use. If you want to persist, use \`zhh k\` and try again."
        return 3
    else
        echo "TPU Unkwown Error, retrying..."
        return 3
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
            kill_tpu $VM_NAME $ZONE && ret=0 || ret=$?
        else
            echo "[INFO] Not killing the TPU process. Exiting."
            return 3
        fi
    fi
    check_env $VM_NAME $ZONE && ret=0 || ret=$?
    if [ $ret -ne 0 ]; then
        echo -e "\033[31m[Error] Environment setup failed. Use \`SCRIPT_DEBUG=1\` for more info.\033[0m"
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

    KILLER=$(cat $ZHH_SCRIPT_ROOT/scripts/new_killer.sh)

    sleep 2
    gcloud compute tpus tpu-vm ssh $VM_NAME --zone=$ZONE --worker=all --command "
    $KILLER
    echo job killed
    "
}
