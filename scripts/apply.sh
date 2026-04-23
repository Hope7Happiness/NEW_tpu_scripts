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

get_service_account(){
#     REGION_SA_MAP = {
#     "us-central1": "bucket-us-central1@he-vision-group.iam.gserviceaccount.com",
#     "us-central2": "bucket-us-central2@he-vision-group.iam.gserviceaccount.com",
#     "us-east1": "373438850578-compute@developer.gserviceaccount.com",
#     "us-east5": "bucket-us-east5@he-vision-group.iam.gserviceaccount.com",
#     "asia-northeast1": "bucket-asia@he-vision-group.iam.gserviceaccount.com",
#     "europe-west4": "373438850578-compute@developer.gserviceaccount.com"
# }
    case $ZONE in
        *us-central1*)
            echo "bucket-us-central1@he-vision-group.iam.gserviceaccount.com"
            ;;
        *us-central2*)
            echo "bucket-us-central2@he-vision-group.iam.gserviceaccount.com"
            ;;
        *us-east1*)
            # echo "373438850578-compute@developer.gserviceaccount.com"
            echo -e "\033[31m[Internal Error] zone $ZONE not supported. Contact admin.\033[0m"
            return 1
            ;;
        *us-east5*)
            echo "bucket-us-east5@he-vision-group.iam.gserviceaccount.com"
            ;;
        *asia-northeast1*)
            echo "bucket-asia@he-vision-group.iam.gserviceaccount.com"
            ;;
        *europe-west4*)
            echo "bucket-europe@he-vision-group.iam.gserviceaccount.com"
            # echo "373438850578-compute@developer.gserviceaccount.com"
            # echo -e "\033[31m[Internal Error] zone $ZONE not supported. Contact admin.\033[0m"
            # return 1
            ;;
        *)
            echo -e "\033[31m[Internal Error] Cannot parse service account from zone $ZONE. Contact admin.\033[0m"
            return 1
            ;;
    esac
}


# the legacy way of using "create"
# get_tpu(){
get_tpu_legacy(){
    VM_NAME=$1
    ZONE=$2
    local apply_log_file=""
    local describe_cmd=""
    local delete_cmd=""
    local max_apply_rounds="${ZHH_MAX_APPLY_ROUNDS:-5}"
    local success=0
    
    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    # get accelerator args
    accelerator_type=$(get_accelerator_args $VM_NAME)
    accelerator_version=$(get_accelerator_version $VM_NAME)
    service_account=$(get_service_account $ZONE)
    if [ $? -ne 0 ]; then
        if [ -n "$apply_log_file" ]; then
            zhh_step_fail "[FAILED]"
            zhh_note "Log: $apply_log_file"
        fi
        return 1
    fi
    create_cmd="gcloud compute tpus tpu-vm create $VM_NAME --zone=$ZONE --accelerator-type=$accelerator_type --version=$accelerator_version --spot --service-account=$service_account"
    describe_cmd="gcloud compute tpus tpu-vm describe $VM_NAME --zone=$ZONE --format=\"value(state)\" 2>/dev/null"
    delete_cmd="gcloud compute tpus tpu-vm delete $VM_NAME --zone=$ZONE --quiet"

    outer_loop=1
    try_start=$(date)
    while [ $outer_loop -le $max_apply_rounds ]; do
        apply_log_file=""
        if zhh_prepare_ring_log_file apply_log_file "apply_tpu" 5 2>/dev/null; then
            zhh_step_banner "Apply TPU" "$apply_log_file" \
                "$(zhh_format_detail "target" "$(zhh_blue_text "$VM_NAME") @ $(zhh_blue_text "$ZONE")")"
            zhh_step_start_spinner
        else
            echo "[INFO] requesting tpu vm $VM_NAME in $ZONE..."
        fi

        if [ -n "$apply_log_file" ]; then
            zhh_capture_eval_output status "$apply_log_file" "$describe_cmd" || true
        else
            status=$(eval "$describe_cmd")
        fi
        if [ "$status" = "READY" ]; then
            if [ -n "$apply_log_file" ]; then
                zhh_step_done
            else
                echo -e "\033[32m[INFO] TPU VM is ready.\033[0m"
            fi
            return 0
        elif [ -z "$status" ]; then
            :
        elif [ "$status" = "PREEMPTED" ]; then
            if [ -n "$apply_log_file" ]; then
                zhh_run_eval_logged "$apply_log_file" "$delete_cmd" || true
            else
                echo "[INFO] TPU VM is preempted. Deleting..."
                gcloud compute tpus tpu-vm delete $VM_NAME --zone=$ZONE --quiet
            fi
        else
            if [ -z "$apply_log_file" ]; then
                echo "[INFO] TPU VM status: $status. Waiting..."
            fi
            sleep 10 # Wait for 1 minutes before checking again
            continue
        fi
        success=0
        for i in {1..3}; do
            if [ -n "$apply_log_file" ]; then
                if zhh_run_eval_logged "$apply_log_file" "$create_cmd"; then
                    success=1
                    break
                fi
            else
                echo "[INFO] Creating TPU VM... Round $outer_loop Attempt $i (time: " $(date) ")"
                if eval $create_cmd ; then
                    echo -e "\033[32m[INFO] TPU VM created successfully.\033[0m"
                    success=1
                    break
                fi
            fi

            # all other calls are quiet
            if [ $i -eq 1 ] && [ $outer_loop -eq 1 ]; then create_cmd="$create_cmd --quiet 2>/dev/null"; fi

            if [ -z "$apply_log_file" ]; then
                echo "[INFO] Failed to create TPU VM. Retrying..."
            fi
        done
        if [ $success -eq 1 ]; then
            if [ -n "$apply_log_file" ]; then
                zhh_step_done
            else
                echo -e "\033[32m[INFO] TPU VM $VM_NAME created successfully.\033[0m"
            fi
            # if available, send email
            semail --apply-success $VM_NAME "$try_start" "$(date)" $outer_loop
            # for this case, TPU must be set up
            # export DO_TPU_SETUP=1
            export TPU_IS_NEW=1
            return
        fi
        # sleep 60 # Wait for 1 minutes before checking again

        # if outer_loop % 100 == 0, send email
        if [ $((outer_loop % 100)) -eq 0 ]; then
            semail --apply-fail $VM_NAME "$try_start" "$(date)" $outer_loop
        fi

        if [ -n "$apply_log_file" ]; then
            zhh_step_fail "[FAILED]"
            zhh_error "Failed to apply after round $outer_loop."
            zhh_note "Log: $apply_log_file"
            if [ $outer_loop -lt $max_apply_rounds ]; then
                zhh_note "Will continue to apply."
            fi
        else
            echo -e "\033[31m[ERROR] Failed to create TPU VM after round $outer_loop.\033[0m"
        fi

        if [ $outer_loop -ge $max_apply_rounds ]; then
            deregister_tpu $VM_NAME
            return 1
        fi

        outer_loop=$((outer_loop+1))

    done;
}


# # now we use queued resources
get_tpu_queue(){
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
    service_account=$(get_service_account $ZONE)
    if [ $? -ne 0 ]; then
        return 1
    fi
    rnd_name=$(head /dev/urandom | tr -dc a-z0-9 | head -c4)
    queue_name=${VM_NAME}_q${rnd_name}

    create_cmd="gcloud compute tpus queued-resources create $queue_name --node-id $VM_NAME --zone $ZONE --accelerator-type=$accelerator_type --runtime-version=$accelerator_version --service-account=$service_account --spot"

    outer_loop=0
    try_start=$(date)
    while true; do
        echo "[INFO] Checking TPU queue for $queue_name (round $outer_loop)..."
        status=$(gcloud compute tpus queued-resources describe $queue_name --zone $ZONE --format="value(state)" 2>&1 )
        # if NOT_FOUND in status
        # status is: state=xxx
        # status=$(echo $status | awk -F'= ' '{print $2}')
        if [[ $status == *"NOT_FOUND"* ]]; then
            echo "[INFO] TPU queue $queue_name does not exist. Creating..."
            for i in {1..100}; do
                echo "[INFO] Creating TPU queue... Attempt $i (time: " $(date) ")"
                if eval $create_cmd ; then
                    echo -e "\033[32m[INFO] TPU queue created successfully.\033[0m"
                    break
                fi

                if [ $i -eq 1 ]; then create_cmd="$create_cmd --quiet 2>/dev/null"; fi

                echo "[INFO] Likely quota exceeded. Failed to create TPU queue. Retrying in 30 seconds..."
                sleep 300 # Wait for 5 min before retrying
            done;
        elif [[ "$status" == *"ACTIVE"* ]]; then
            echo -e "\033[32m[INFO] TPU $VM_NAME @ $ZONE is created.\033[0m"
            semail --apply-success $VM_NAME "$try_start" "$(date)" $outer_loop
            break
        elif [[ "$status" == *"FAILED"* ]] || [[ "$status" == *"SUSPENDING"* ]] || [[ "$status" == *"SUSPENDED"* ]]; then
            echo "[INFO] TPU queue creation failed. Deleting and retrying..."
            gcloud compute tpus queued-resources delete $queue_name --zone=$ZONE --quiet
            if [[ $? -ne 0 ]]; then
                echo "[WARNING] Failed to delete TPU queue $queue_name. Continuing..."
            fi
        else
            echo "[INFO] TPU queue status: $status. Waiting..."
        fi

        sleep 60 # Wait for 1 minutes before checking again
        outer_loop=$((outer_loop+1))
        if [ $((outer_loop % 100)) -eq 0 ]; then
            semail --apply-fail $VM_NAME "$try_start" "$(date)" $outer_loop
        fi
    done;
}

get_tpu_parallel(){
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
    service_account=$(get_service_account $ZONE)
    if [ $? -ne 0 ]; then
        return 1
    fi
    create_cmd="gcloud compute tpus tpu-vm create $VM_NAME --zone=$ZONE --accelerator-type=$accelerator_type --version=$accelerator_version --spot --service-account=$service_account"

    while_create_cmd="while true; do $create_cmd; sleep 30; done"

    # open 8 parallel process to run the same command, log to /tmp/apply_$VM_NAME_pid.log
    echo "[INFO] Starting 8 parallel processes to create TPU VM..."
    for i in {1..8}; do
        # sleep a random time between 0 and 30 seconds to avoid all processes start at the same time
        sleep $((RANDOM % 30))
        (
            eval $while_create_cmd > /tmp/apply_${VM_NAME}_$i.log 2>&1
        ) &
    done

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

        # if outer loop is 0, display all logs
        if [ $outer_loop -eq 0 ]; then
            echo "[INFO] Displaying logs from parallel processes:"
            for i in {1..8}; do
                echo "----- Log from process $i -----"
                cat /tmp/apply_${VM_NAME}_$i.log
                echo "-------------------------------"
            done
        fi

        sleep 60 # Wait for 1 minutes before checking again
        outer_loop=$((outer_loop+1))
        # if outer_loop % 100 == 0, send email
    done;

    # kill all parallel processes
    echo "[INFO] Killing all parallel processes..."
    pkill -f "apply_${VM_NAME}_"
}

get_tpu(){

    # if $2 is empty or "INTERNAL ERROR", then the system is bugged, directly exit
    if [ -z "$2" ] || [[ "$2" == *"INTERNAL"* ]]; then
        echo -e "\033[31m[Internal Error] Previous TPU VM $1 is in bad state. Please check the logs and contact admin.\033[0m"
        return 1
    fi

    trap 'zhh_cleanup_ui; echo -e "\n\033[33m[Info] Caught interrupt signal. Deregistering...\033[0m"; deregister_tpu $1; exit $ret' INT
    if [ "$USE_QUEUE" = "1" ]; then
        # get_tpu_queue $1 $2
        echo -e 'NO LONGER USE QUEUE. Please set USE_QUEUE=0 and use legacy get_tpu.\n'
        return 1
    elif [ "$USE_PARALLEL" = "1" ]; then
        # get_tpu_parallel $1 $2
        echo -e 'NO LONGER USE PARALLEL. Please set USE_QUEUE=0 and use legacy get_tpu.\n'
        return 1
    else
        get_tpu_legacy $1 $2 && ret=0 || ret=$?
    fi
    trap - INT
    return $ret
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
    elif [[ -z "$status" || "$status" = "DELETED" || "$status" = "PREEMPTED" ]]; then
        # log_tpu_check_result deleted
        deregister_tpu $VM_NAME
        return 1
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
        deregister_tpu $VM_NAME
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

run_helpzak(){
    if [ -n "$ZHH_ENV_CHECK_LOG_FILE" ]; then
        zhh_run_logged_command "$ZHH_ENV_CHECK_LOG_FILE" gcloud compute tpus tpu-vm ssh kmh-tpuvm-v4-8-3 --zone=us-central2-b --command="sudo -iu sqa sudo -iu sqa bash /home/sqa/.helpzak $VM_NAME $ZONE"
    else
        gcloud compute tpus tpu-vm ssh kmh-tpuvm-v4-8-3 --zone=us-central2-b --command="sudo -iu sqa sudo -iu sqa bash /home/sqa/.helpzak $VM_NAME $ZONE"
    fi
}

setup_tpu(){
    # ZHH: change setup_tpu to must use setup
    log_tpu_check_result "testing"

    if [ "$TPU_IS_NEW" = "1" ]; then
        zhh_muted_info "TPU is newly created. Installing runtime dependencies."
        run_setup_script $VM_NAME $ZONE
    else
        zhh_muted_info "TPU already exists. Skipping install unless environment check requests it."
    fi
    while_check_env $VM_NAME $ZONE && ret=0 || ret=$?
    if [ $ret -eq 9 ]; then
        zhh_warn "TPU may be preempted during environment check. Trying helpzak once."
        run_helpzak
        while_check_env $VM_NAME $ZONE && ret=0 || ret=$?
        if [ $ret -eq 9 ]; then
            zhh_warn "TPU is still preempted after helpzak."
            return 9
        fi
        # return 9
    elif [ $ret -ne 0 ]; then
        zhh_warn "Environment check failed with ret=$ret."
        return $ret
    fi
    run_wandb_login $VM_NAME $ZONE && ret=0 || ret=$?
    if [ $ret -eq 9 ]; then
        zhh_warn "TPU may be preempted during wandb login. Re-applying."
        return 9
    elif [ $ret -ne 0 ]; then
        zhh_warn "Wandb login failed with ret=$ret."
        return $ret
    fi

    # success
    log_tpu_check_result "good"
    return 0
}

get_and_setup_tpu(){
    # write name lock (group shared):
    # zak_$VM_NAME_2026-02-26_21-42-42
    printf '%bℹ️  Preparing TPU %s @ %s%b\n' "$ZHH_COLOR_DIM" "$(zhh_blue_text "$VM_NAME")" "$(zhh_blue_text "$ZONE")" "$ZHH_COLOR_RESET"
    LOCK_FILE="/kmh-nfs-ssd-us-mount/code/qiao/tpu_lock/zak_${VM_NAME}_$(date -u +%Y-%m-%d_%H-%M-%S)"
    sudo touch $LOCK_FILE

    if [ ! -z "$FAST_DEBUG" ]; then
        zhh_warn "FAST_DEBUG is set. Skipping TPU allocation and setup."
        # unset FAST_DEBUG # if for second time (i.e. card preempted), then do normal
        return 0
    fi

    ret=9
    trial=0
    while [ $ret -eq 9 ]; do
        export ZHH_SETUP_TRIAL=$((trial+1))
        zhh_box_section "TPU setup trial ${ZHH_SETUP_TRIAL}/5"
        get_tpu $VM_NAME $ZONE && ret=0 || ret=$?
        if [ $ret -ne 0 ]; then
            deregister_tpu $VM_NAME
            unset ZHH_SETUP_TRIAL
            return 42
        fi
        setup_tpu $VM_NAME $ZONE && ret=0 || ret=$?
        if [ $ret -eq 0 ]; then
            zhh_box_success "TPU $VM_NAME @ $ZONE is ready to use."
            unset ZHH_SETUP_TRIAL
            return 0
        fi
        trial=$((trial+1))
        export SCRIPT_DEBUG=1 # for the subsequent runs, always use verbose mode
        if [ $trial -ge 5 ]; then
            zhh_error "TPU $VM_NAME @ $ZONE setup failed after 5 trials."
            deregister_tpu $VM_NAME
            unset ZHH_SETUP_TRIAL
            return 1
        fi
        zhh_warn "Retrying TPU setup in 60 seconds."
        sleep 60
    done
    unset ZHH_SETUP_TRIAL
    zhh_error "get_and_setup_tpu exited with ret=$ret"
    zhh_info "Automatically switching to another card."
    deregister_tpu $VM_NAME
    return 42
    # return $ret
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
