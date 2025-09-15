source $ZHH_SCRIPT_ROOT/scripts/setup.sh

if [ "$DO_TPU_SETUP" = "1" ]; then
    echo -e "\033[33m[Env Hint] TPU setup will be performed.\033[0m"
else
    echo -e "\033[33m[Env Hint] TPU setup will be skipped.\033[0m"
fi

alias semail="python3 /kmh-nfs-us-mount/code/siri/pemail.py"

get_tpu(){
    VM_NAME=$1
    ZONE=$2
    
    echo requesting tpu vm $VM_NAME in $ZONE...

    if [ -z "$VM_NAME" ]; then
        echo "Error: VM_NAME is not set."
        return 1
    fi
    outer_loop=0
    try_start=$(date)
    while true; do
        status=$(
            gcloud compute tpus tpu-vm describe $VM_NAME --zone=$ZONE --format="value(state)"
        )
        if [ "$status" = "READY" ]; then
            echo "TPU VM is ready."
            break
        elif [ -z "$status" ]; then
            echo "TPU VM does not exist."
        elif [ "$status" = "PREEMPTED" ]; then
            echo "TPU VM is preempted. Deleting..."
            gcloud compute tpus tpu-vm delete $VM_NAME --zone=$ZONE --quiet
        else
            echo "TPU VM status: $status. Waiting..."
            continue
        fi
        success=0
        for i in {1..3}; do
            echo "Creating TPU VM... Round $outer_loop Attempt $i (time: " $(date) ")"
            if gcloud compute tpus tpu-vm create $VM_NAME --zone=$ZONE --accelerator-type=v4-32 --version=tpu-ubuntu2204-base --spot --quiet 2>/dev/null ; then
                echo "TPU VM created successfully."
                success=1
                break
            fi
            echo "Failed to create TPU VM. Retrying in 10 seconds..."
            sleep 10 # Wait for 10 seconds before retrying
        done
        if [ $success -eq 1 ]; then
            echo "TPU VM $VM_NAME created successfully."
            # if available, send email
            semail $VM_NAME $try_start "$(date)" $outer_loop || echo -e "\033[33m[Warning] Failed to send email.\033[0m"
            # for this case, TPU must be set up
            export DO_TPU_SETUP=1
            return
        fi
        sleep 60 # Wait for 1 minutes before checking again
        outer_loop=$((outer_loop+1))
        # if outer_loop % 100 == 0, send email
        if [ $((outer_loop % 100)) -eq 0 ]; then
            semail $VM_NAME $try_start "$(date)" $outer_loop --fail || echo -e "\033[33m[Warning] Failed to send email.\033[0m"
        fi
    done;
}

setup_tpu(){
    VM_NAME=$1
    ZONE=$2

    echo setting up tpu vm $VM_NAME in $ZONE...

    if [ -z "$VM_NAME" ]; then
        echo "Error: VM_NAME is not set."
        return 1
    fi

    if [ "$DO_TPU_SETUP" = "1" ]; then
        mount_disk $VM_NAME $ZONE && \
        setup_env $VM_NAME $ZONE
    else
        echo "Skipping TPU environment setup as DO_TPU_SETUP is not set."
        check_env $VM_NAME $ZONE
    fi
}

kill_tpu(){
    VM_NAME=$1
    ZONE=$2

    echo killing tpu vm $VM_NAME in $ZONE...

    if [ -z "$VM_NAME" ]; then
        echo "Error: VM_NAME is not set."
        return 1
    fi

    sleep 2
    gcloud compute tpus tpu-vm ssh $VM_NAME --zone=$ZONE --worker=all --command "
    ps -ef | grep main.py | grep -v grep | awk '{print \"kill -9 \" \$2}'
    ps -ef | grep main.py | grep -v grep | awk '{print \"kill -9 \" \$2}' | sh
    echo job killed
    "
}

check_and_kill(){
    VM_NAME=$1
    ZONE=$2

    if [ -z "$VM_NAME" ]; then
        echo "Error: VM_NAME is not set."
        return 1
    fi

    check_env $VM_NAME $ZONE || kill_tpu $VM_NAME $ZONE
}