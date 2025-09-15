mount_disk(){
    VM_NAME=$1
    ZONE=$2

    level=0
    
    while true; do
        # test if the disk is already mounted
        gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE \
            --worker=all --command "ls /kmh-nfs-us-mount/code/siri"
        if [ $? -eq 0 ]; then
            echo "Disk is already mounted."
            break
        fi

        level=$((level+1))

        if [ $level -gt 1 ]; then 

            # more advanced check
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
    VM_NAME=$1
    ZONE=$2

    if [ -z "$VM_NAME" ]; then
        echo "Error: VM_NAME is not set."
        return 1
    fi

    TEST="sudo rm -rf /tmp/tpu_logs; python3 -c 'import jax; print(jax.devices())'"
    result=$(gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE \
    --worker=all --command "$TEST")
    if [[ $result == *"TpuDevice"* ]]; then
        echo "Environment setup successful."
    else
        echo "Environment setup failed. Retrying..."
        return 1
    fi
}

setup_env(){
    VM_NAME=$1
    ZONE=$2

    if [ -z "$WANDB_API_KEY" ]; then
        echo "Error: WANDB_API_KEY is not set. Please set WANDB_API_KEY in ka.sh."
        return 1
    fi

    COMMAND=$(cat $ZHH_SCRIPT_ROOT/scripts/install.sh)
    COMMAND="$COMMAND
    python -m wandb login $WANDB_API_KEY
    "

    # pip install step
    gcloud compute tpus tpu-vm ssh $VM_NAME --zone $ZONE \
    --worker=all --command "$COMMAND"
    if [ $? -ne 0 ]; then
        echo "Environment setup failed during pip install:"
        return 1
    fi

    check_env $VM_NAME $ZONE
}