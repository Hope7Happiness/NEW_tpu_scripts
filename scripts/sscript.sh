# Managing TPU Status

source $ZHH_SCRIPT_ROOT/scripts/common.sh

SSCRIPT_HOME=/kmh-nfs-ssd-us-mount/staging/.sscript

log_command(){
    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    COMMAND=$1

    sudo mkdir -p $SSCRIPT_HOME/$VM_NAME && \
    (echo "$COMMAND" | sudo tee $SSCRIPT_HOME/$VM_NAME/command) > /dev/null
    (echo "STARTED" | sudo tee $SSCRIPT_HOME/$VM_NAME/status) > /dev/null
}

log_notes(){
    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    NOTES=$1

    sudo mkdir -p $SSCRIPT_HOME/$VM_NAME && \
    (echo "$NOTES" | sudo tee $SSCRIPT_HOME/$VM_NAME/notes) > /dev/null
}

fail_command(){
    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    sudo mkdir -p $SSCRIPT_HOME/$VM_NAME && \
    (echo "FAILED" | sudo tee $SSCRIPT_HOME/$VM_NAME/status) > /dev/null
}

killed_command(){
    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    sudo mkdir -p $SSCRIPT_HOME/$VM_NAME && \
    echo "KILLED" | sudo tee $SSCRIPT_HOME/$VM_NAME/status
}

success_command(){
    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    sudo mkdir -p $SSCRIPT_HOME/$VM_NAME && \
    (echo "FINISHED" | sudo tee $SSCRIPT_HOME/$VM_NAME/status) > /dev/null
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

has_failure(){
    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    if [ ! -f $SSCRIPT_HOME/$VM_NAME/status ]; then
        return 1
    fi

    status=$(sudo cat $SSCRIPT_HOME/$VM_NAME/status)
    if [ "$status" = "FAILED" ]; then
        return 0
    else
        return 1
    fi
}

list_tpus(){
    # ls $SSCRIPT_HOME
    for folder in $SSCRIPT_HOME/*; do
        vm_name=$(basename $folder)
        zone=$(cat $folder/zone 2>/dev/null || echo "INTERNAL ERROR")
        echo -e "$vm_name $zone"
    done;
}

register_tpu(){
    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    if [ -z "$ZONE" ]; then
        echo -e "\033[31m[Internal Error] ZONE is unset.\033[0m"
        return 1
    fi

    sudo mkdir -p $SSCRIPT_HOME/$VM_NAME && \
    (echo "$ZONE" | sudo tee $SSCRIPT_HOME/$VM_NAME/zone) > /dev/null
    (echo "ready" | sudo tee $SSCRIPT_HOME/$VM_NAME/check_result) > /dev/null
}

deregister_tpu(){
    if [ -z "$1" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    sudo rm -rf $SSCRIPT_HOME/$1
}

log_tpu_check_result(){
    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    RESULT=$1

    sudo mkdir -p $SSCRIPT_HOME/$VM_NAME && \
    (echo "$RESULT" | sudo tee $SSCRIPT_HOME/$VM_NAME/check_result) > /dev/null
}

get_tpu_check_result(){
    if [ -z "$1" ]; then
        echo -e "\033[31m[Internal Error] VM name arg is missing.\033[0m"
        return 1
    fi

    cat $SSCRIPT_HOME/$1/check_result 2>/dev/null || echo "NO CHECK RESULT"
}

show_all_tpu_status(){
    MSGS=()
    for folder in $SSCRIPT_HOME/*; do
        raw_vm_name=$(basename $folder)
        raw_command=$(cat $folder/command 2>/dev/null || echo "NO COMMAND FOUND")
        vm_zone=$(cat $folder/zone 2>/dev/null || echo "NO ZONE FOUND")

        # highlight vm name
        vm_name=$(echo $raw_vm_name | sed -E 's/kmh-tpuvm-(v[^-]+-[0-9]+)-(.+)([0-9]+)/kmh-tpuvm-\\033[34m\1\\033[0m-\2\\033[32m\3\\033[0m/')

        # highlight command
        # for str like *staging/\w+/(\w+)/launch*, highlight the (\w+)
        # command=$(echo $raw_command | sed -E 's#(staging/\w+/)(\w+)(/launch)#\1\\033[33m\2\\033[0m\3#g')
        workdir=$(echo $raw_command | grep -oE -- '--workdir=[^ ]+' | sed 's#--workdir=##g')
        # highlight workdir part
        workdir_hl=$(echo $workdir | sed -E 's#(staging/\w+/)(\w+)(/launch_.*)#\1\\033[33m\2\\033[0m\3#g')
        # update: now use notes
        notes=$(cat $folder/notes 2>/dev/null || echo "wandb notes not found")


        # grep log dir: --workdir=/kmh-nfs-us-mount/staging/siri/mf_rev/launch_20250917_203208_gitfd6ce86_f72f4085/logs/log1_20250917_203225_9912f3e1/output.log
        log_dir=$(echo $raw_command | grep -oE -- '--workdir=[^ ]+' | sed 's#--workdir=##g')
        log_file="$log_dir/output.log"

        raw_status=$(cat $SSCRIPT_HOME/$raw_vm_name/status 2>/dev/null || echo "UNKNOWN")
        status=$(echo $raw_status | sed -E 's/STARTED/\\033[34m&\\033[0m/g' | sed -E 's/FAILED/\\033[31m&\\033[0m/g' | sed -E 's/FINISHED/\\033[32m&\\033[0m/g' | sed -E 's/KILLED/\\033[33m&\\033[0m/g')

        raw_tpu_check_result=$(get_tpu_check_result $raw_vm_name)
        tpu_check_result=$(echo $raw_tpu_check_result | sed -E 's/ready/\\033[32m&\\033[0m/g' | sed -E 's/deleted/\\033[31m&\\033[0m/g' | sed -E 's/in\ use/\\033[33m&\\033[0m/g')

        # if no log for 30 min, switch "STARTED" to "STALED"
        # grep last log time from logdir
        # I0918 00:10:41.255399 139818289895424
        last_time=$(cat $log_file 2>/dev/null | grep -a -oE '^[IWE][0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]+' | tail -n 1 | awk '{print $1, $2}')
        if [ ! -z "$last_time" ]; then
            last_epoch=$(date -d "$(date +%Y)${last_time:1:4} ${last_time:6:8}" +%s)
            now_epoch=$(date +%s)
            diff_min=$(( (now_epoch - last_epoch) / 60 ))
            if [ $diff_min -ge 30 ]; then
                # if status is STARTED, switch to STALED
                if [ "$raw_status" = "STARTED" ]; then
                    status="\033[33mSTALED\033[0m"
                fi
            fi

            # convert unit
            diff_msg="$diff_min min"
            if [ $diff_min -ge 1440 ]; then
                diff_msg="$((diff_min / 1440)) days"
            fi
        fi

        # echo -e "\n[$status] (last log: $diff_msg ago) \033[1m$vm_name @ $vm_zone\033[0m ($tpu_check_result) -> $notes\n\t===> check at $workdir_hl/output.log"
        MSGS+=("\n[$status] (last log: $diff_msg ago) \033[1m$vm_name @ $vm_zone\033[0m ($tpu_check_result) -> $notes\n\t===> check at $workdir_hl/output.log")
    done;
    # sort msgs (gpt)
    mapfile -d '' -t sorted_msgs < <(printf '%s\0' "${MSGS[@]}" | sort -z)
    printf '%b\n' "${sorted_msgs[@]}"

    echo -e "\n\033[1mHint\033[0m: The TPU status may not be new. Use \`zhh wall\` to refresh."
}

# Queue Management

queue_job(){
    # assert 1 arg
    if [ "$#" -ne 1 ]; then
        echo -e "\033[31m[Internal Error] Wrong number of args\033[0m"
        return 1
    fi
    STAGE_DIR=$1
    sudo mkdir -p $SSCRIPT_HOME/$VM_NAME/queue
    sudo mkdir -p $SSCRIPT_HOME/$VM_NAME/queue.fifo

    # write STAGE_DIR using date
    NOW=$(date +"%Y%m%d_%H%M%S")
    (echo $STAGE_DIR | sudo tee $SSCRIPT_HOME/$VM_NAME/queue/$NOW) > /dev/null

    # make a FIFO
    FIFO=$SSCRIPT_HOME/$VM_NAME/queue.fifo/$NOW
    [[ -p $FIFO ]] || sudo mkfifo $FIFO && sudo chmod 666 $FIFO

    echo -e "\033[32m[Info] Queued job $STAGE_DIR at $NOW. Now, the program will stuck, which is EXPECTED. If you want to dequeue, just press Ctrl+C.\033[0m"

    (
        # exec 3<"$FIFO"
        trap "echo 'Interrupted, finishing...'; sudo rm -f $SSCRIPT_HOME/$VM_NAME/queue/$NOW" EXIT # remove record
        read -r msg <"$FIFO"

        if [ "$msg" == "START" ]; then
            echo -e "\033[32m[Info] Job $STAGE_DIR is starting.\033[0m"
        else
            echo -e "\033[33m[Internal WARNING] Got message $msg, expected to be START\033[0m"
            # return 2
        fi
        # exec 3<&-
    )

    semail --queue-start $STAGE_DIR $NOW "$(date)" $VM_NAME
}

release_queue(){
    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    # check if there is any job in queue
    if queue_isempty; then
        echo -e "\033[33m[Info] No job in queue to release.\033[0m"
        return 0
    fi

    if [ ! -d $SSCRIPT_HOME/$VM_NAME/queue.fifo ]; then
        echo -e "\033[31m[Internal Error] FIFO directory not found.\033[0m"
        return 1
    fi

    item=$(ls $SSCRIPT_HOME/$VM_NAME/queue | head -n 1)

    FIFO=$SSCRIPT_HOME/$VM_NAME/queue.fifo/$item
    [[ -p $FIFO ]] || sudo mkfifo $FIFO && sudo chmod 666 $FIFO

    # exec 3>"$FIFO"
    timeout 10s bash -c "printf 'START\\n' > \"$FIFO\"" || {
        echo -e "\033[31m[Internal Error] Timeout when releasing queue. The job might not start. Please check manually.\033[0m"
        return 1
    }

    # timeout 1s printf 'START\n' >&3 || {
    #     echo -e "\033[31m[Internal Error] Timeout when releasing queue. The job might not start. Please check manually.\033[0m"
    #     exec 3>&-
    #     return 1
    # }
    # exec 3>&-
    echo -e "\033[32m[Info] Started job id $item.\033[0m"
}

queue_isempty(){
    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    if [ ! -d $SSCRIPT_HOME/$VM_NAME/queue ] || [ -z "$(ls -A $SSCRIPT_HOME/$VM_NAME/queue)" ]; then
        return 0
    else
        return 1
    fi
}

show_queue_status(){
    echo -e "\033[1mQueued jobs:\033[0m"
    for folder in $SSCRIPT_HOME/*; do
        vm_name=$(basename $folder)
        echo -e "\t$vm_name:"
        for q in $(ls $SSCRIPT_HOME/$vm_name/queue/* 2>/dev/null); do
            job_id=$(basename $q)
            stage_dir=$(cat $q)
            echo -e "\t\t$job_id --> $stage_dir"
        done;
        echo;
    done;
}