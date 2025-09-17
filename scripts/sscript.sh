# Managing TPU Status

source $ZHH_SCRIPT_ROOT/scripts/common.sh

SSCRIPT_HOME=/kmh-nfs-us-mount/staging/.sscript

log_command(){
    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    COMMAND=$1

    sudo mkdir -p $SSCRIPT_HOME/$VM_NAME && \
    echo "$COMMAND" | sudo tee $SSCRIPT_HOME/$VM_NAME/command
    echo -e "\033[32mSTARTED\033[0m" | sudo tee $SSCRIPT_HOME/$VM_NAME/status
}

fail_command(){
    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    sudo mkdir -p $SSCRIPT_HOME/$VM_NAME && \
    echo "FAILED" | sudo tee $SSCRIPT_HOME/$VM_NAME/status
}

success_command(){
    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    sudo mkdir -p $SSCRIPT_HOME/$VM_NAME && \
    echo "FINISHED" | sudo tee $SSCRIPT_HOME/$VM_NAME/status
}

get_command(){
    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    if [ ! -f $SSCRIPT_HOME/$VM_NAME/command ]; then
        echo -e "\033[33m[Warning] No command found for $VM_NAME\033[0m"
        return 1
    fi

    sudo cat $SSCRIPT_HOME/$VM_NAME/command
}

queue_job(){
    # assert 1 arg
    if [ "$#" -ne 1 ]; then
        echo -e "\033[31m[Internal Error] Wrong number of args\033[0m"
        return 1
    fi
    STAGE_DIR=$1
    sudo mkdir -p $SSCRIPT_HOME/$VM_NAME/queue

    # make a FIFO
    FIFO=$SSCRIPT_HOME/$VM_NAME/queue.fifo
    [[ -p $FIFO ]] || sudo mkfifo $FIFO && sudo chmod 666 $FIFO

    # write STAGE_DIR using date
    NOW=$(date +"%Y%m%d_%H%M%S")
    echo $STAGE_DIR | sudo tee $SSCRIPT_HOME/$VM_NAME/queue/$NOW # This is only for record

    echo -e "\033[32m[Info] Queued job $STAGE_DIR at $NOW. Now, the program will stuck, which is EXPECTED. If you want to dequeue, just press Ctrl+C.\033[0m"

    exec 3<>"$FIFO"

    (
        trap "echo 'Interrupted, finishing...'; sudo rm -f $SSCRIPT_HOME/$VM_NAME/queue/$NOW" EXIT # remove record
        read -r msg <&3

        if [ "$msg" == "START" ]; then
            echo -e "\033[32m[Info] Job $STAGE_DIR is starting.\033[0m"
        else
            echo -e "\033[33m[Internal WARNING] Got message $msg, expected to be START\033[0m"
            # return 2
        fi
    )
    
    semail --queue-start $STAGE_DIR $NOW "$(date)" $VM_NAME
}

release_queue(){
    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    FIFO=$SSCRIPT_HOME/$VM_NAME/queue.fifo
    [[ -p $FIFO ]] || sudo mkfifo $FIFO && sudo chmod 666 $FIFO

    # # check if there is any job in queue
    # if [ ! -d $SSCRIPT_HOME/$VM_NAME/queue ] || [ -z "$(ls -A $SSCRIPT_HOME/$VM_NAME/queue)" ]; then
    #     echo -e "\033[33m[Info] No job in queue to release.\033[0m"
    #     return 0
    # fi

    exec 3<>"$FIFO"
    printf 'START\n' >&3
    exec 3>&- 3<&- # ensure exit of this process
}