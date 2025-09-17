source $ZHH_SCRIPT_ROOT/scripts/common.sh

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
        gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE \
            --worker=all --command "ls /kmh-nfs-us-mount/code/siri" > /dev/null
        if [ $? -eq 0 ]; then
            echo "Disk is already mounted."
            break
        fi

        level=$((level+1))

        if [ $level -gt 1 ]; then 

            # more advanced mount op
            gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE \
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

        fi

        # standard mount op
        gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE \
        --worker=all --command "
        sleep 8
        sudo mkdir -p /kmh-nfs-us-mount
        sudo mount -o vers=3 10.26.72.146:/kmh_nfs_us /kmh-nfs-us-mount
        sudo chmod go+rw /kmh-nfs-us-mount
        ls /kmh-nfs-us-mount

        sudo mkdir -p /kmh-nfs-ssd-eu-mount
        sudo mount -o vers=3 10.150.179.250:/kmh_nfs_ssd_eu /kmh-nfs-ssd-eu-mount
        sudo chmod go+rw /kmh-nfs-ssd-eu-mount
        ls /kmh-nfs-ssd-eu-mount
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

    TEST="sudo rm -rf /tmp/tpu_logs; python3 -c 'import jax; print(jax.devices())'"
    # read both stdout and stderr
    result=$(gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE \
    --worker=all --command "$TEST" 2>&1)
    if [ ! -z "$SCRIPT_DEBUG" ]; then
        echo "Debug info from environment check:"
        echo "$result"
    fi

    if [[ $result == *"TpuDevice"* ]]; then
        echo "Environment setup successful."
    elif [[ $result == *"jaxlib.xla_extension.XlaRuntimeError: ABORTED: The TPU is already in use by process with pid"* ]]; then
        echo "TPU is already in use. If you want to persist, use \`zhh k\` and try again."
        return 1
    else
        echo "Environment setup failed. Use \`SCRIPT_DEBUG=1\` for more info."
        return 1
    fi
}

setup_env(){
    VM_NAME=$1
    ZONE=$2

    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    COMMAND=$(cat $ZHH_SCRIPT_ROOT/scripts/install.sh)

    # pip install step
    gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE \
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

    if [ -z "$WANDB_API_KEY" ]; then
        echo -e "\033[31m[Internal Error] WANDB_API_KEY is not set. Contact admin.\033[0m"
        # echo -e "\033[31m[Error] WANDB_API_KEY is not set. Please set WANDB_API_KEY in ka.sh.\033[0m"
        return 1
    fi

    COMMAND="python -m wandb login $WANDB_API_KEY"

    # pip install step
    gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE \
    --worker=all --command "$COMMAND" > /dev/null 2>&1
    if [ $? -ne 0 ]; then
        echo -e "\033[31m[Error] Wandb login failed.\033[0m"
        return 1
    fi

    echo "Wandb login successful."
}