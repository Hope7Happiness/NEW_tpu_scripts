# Managing TPU Status

SSCRIPT_HOME=/kmh-nfs-us-mount/staging/.sscript

VM_UNFOUND_ERROR_SSCRIPT="\033[31m[Internal Error] VM_NAME is not set. Contact admin.\033[0m"

log_command(){
    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR_SSCRIPT
        return 1
    fi

    COMMAND=$1

    sudo mkdir -p $SSCRIPT_HOME/$VM_NAME && \
    echo "$COMMAND" > $SSCRIPT_HOME/$VM_NAME/command
    echo -e "\033[32mSTARTED\033[0m" > $SSCRIPT_HOME/$VM_NAME/status
}

fail_command(){
    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR_SSCRIPT
        return 1
    fi

    sudo mkdir -p $SSCRIPT_HOME/$VM_NAME && \
    echo "FAILED" > $SSCRIPT_HOME/$VM_NAME/status
}

success_command(){
    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR_SSCRIPT
        return 1
    fi

    sudo mkdir -p $SSCRIPT_HOME/$VM_NAME && \
    echo "FINISHED" > $SSCRIPT_HOME/$VM_NAME/status
}

get_command(){
    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR_SSCRIPT
        return 1
    fi

    if [ ! -f $SSCRIPT_HOME/$VM_NAME/command ]; then
        echo -e "\033[33m[Warning] No command found for $VM_NAME\033[0m"
        return 1
    fi

    cat $SSCRIPT_HOME/$VM_NAME/command
}