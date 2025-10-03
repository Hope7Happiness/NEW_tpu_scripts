source $ZHH_SCRIPT_ROOT/scripts/common.sh

wrap_gcloud(){
    if [ ! -z "$SCRIPT_DEBUG" ]; then
        gcloud "$@"
    else
        gcloud "$@" > /dev/null 2>&1
    fi
}

mount_disk(){
    VM_NAME=$1
    ZONE=$2

    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    level=0 # if level >= 2, will do more advanced mount op
    
    while true; do
        # test if the disk is already mounted
        wrap_gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE \
            --worker=all --command "sudo umount -l /kmh-nfs-ssd-eu-mount || true; ls /kmh-nfs-ssd-us-mount/code/siri"
        if [ $? -eq 0 ]; then
            echo "Disk is already mounted."
            break
        fi

        level=$((level+1))

        if [ $level -gt 1 ]; then 

            # more advanced mount op
            wrap_gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE \
            --worker=all --command "
            ps -ef | grep -i unattended | grep -v 'grep' | awk '{print \"sudo kill -9 \" \$2}'
            ps -ef | grep -i unattended | grep -v 'grep' | awk '{print \"sudo kill -9 \" \$2}' | sh
            ps -ef | grep -i unattended | grep -v 'grep' | awk '{print \"sudo kill -9 \" \$2}' | sh
            sleep 5
            sudo apt-get -y update
            sudo apt-get -y install nfs-common
            ps -ef | grep -i unattended | grep -v 'grep' | awk '{print \"sudo kill -9 \" \$2}'
            ps -ef | grep -i unattended | grep -v 'grep' | awk '{print \"sudo kill -9 \" \$2}' | sh
            ps -ef | grep -i unattended | grep -v 'grep' | awk '{print \"sudo kill -9 \" \$2}' | sh
            sleep 6
            "

            for i in {1..10}; do echo Mount Mount 妈妈; done
            sleep 7
            if [ $level -gt 3 ]; then
                echo -e "\033[31m[Error] Disk mount failed after multiple attempts. The card is likely PREEMPTED. Please try again.\033[0m"
                return 1
            fi
        fi

        # standard mount op
        wrap_gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE \
        --worker=all --command "
        sleep 8
        sudo mkdir -p /kmh-nfs-ssd-us-mount
        sudo mount -o vers=3 10.97.81.98:/kmh_nfs_ssd_us /kmh-nfs-ssd-us-mount
        sudo chmod go+rw /kmh-nfs-ssd-us-mount
        ls /kmh-nfs-ssd-us-mount
	
	sudo mkdir -p /kmh-nfs-us-mount
	sudo mount -o vers=3 10.26.72.146:/kmh_nfs_us /kmh-nfs-us-mount
	sudo chmod go+rw /kmh-nfs-us-mount
	ls /kmh-nfs-us-mount
	
	"
    done;
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
    result=$(timeout 180s gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE \
    --worker=all --command "$TEST" 2>&1 || true)

    if [[ $result == *"TpuDevice"* ]]; then
        echo "Environment setup successful."
    elif [[ $result == *"jaxlib.xla_extension.XlaRuntimeError: ABORTED: The TPU is already in use by process with pid"* ]]; then
        echo "TPU is already in use. If you want to persist, use \`zhh k\` and try again."
        return 3
    else
        echo "Environment setup failed. Use \`SCRIPT_DEBUG=1\` for more info."
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

    check_env $VM_NAME $ZONE && ret=0 || ret=$?
    if [ $ret -eq 3 ]; then
        read -p "Kill the TPU process right now? (y/n) " yn
        if [ "$yn" = "y" ]; then
            kill_tpu $VM_NAME $ZONE && ret=0 || ret=$?
        fi
    fi
    return $ret
}

setup_env(){
    VM_NAME=$1
    ZONE=$2

    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    # if VM_NAME in *v6*
    if [[ $VM_NAME =~ v6e ]]; then
        COMMAND=$(cat $ZHH_SCRIPT_ROOT/scripts/install_v6e.sh)
    else
        COMMAND=$(cat $ZHH_SCRIPT_ROOT/scripts/install.sh)
    fi

    # pip install step
    wrap_gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE \
    --worker=all --command "$COMMAND"
    if [ $? -ne 0 ]; then
        echo -e "\033[31m[Error] Environment setup failed during pip install:\033[0m"
        return 1
    fi
}

wandb_login(){
    VM_NAME=$1
    ZONE=$2

    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi
    
    py_path=$CONDA_PY_PATH
    # if VM_NAME contains v6, don't use conda
    if [[ $VM_NAME =~ v6e ]]; then
        py_path="python"
    fi

    if [ -z "$WANDB_API_KEY" ]; then
        echo -e "\033[31m[Internal Error] WANDB_API_KEY is not set. Contact admin.\033[0m"
        # echo -e "\033[31m[Error] WANDB_API_KEY is not set. Please set WANDB_API_KEY in ka.sh.\033[0m"
        return 1
    fi

    COMMAND="$py_path -m wandb login $WANDB_API_KEY"

    # pip install step
    wrap_gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE \
    --worker=all --command "$COMMAND"
    if [ $? -ne 0 ]; then
        echo -e "\033[31m[Error] Wandb login failed.\033[0m"
        return 1
    fi

    echo "Wandb login successful."
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
