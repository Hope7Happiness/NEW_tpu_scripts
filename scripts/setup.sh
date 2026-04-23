source $ZHH_SCRIPT_ROOT/scripts/common.sh

wrap_gcloud(){
    if [ -n "$ZHH_CAPTURE_LOG_FILE" ]; then
        zhh_run_logged_command "$ZHH_CAPTURE_LOG_FILE" "$CUSTOM_GCLOUD_EXE" "$@"
    elif [ ! -z "$SCRIPT_DEBUG" ]; then
        "$CUSTOM_GCLOUD_EXE" "$@"
    else
        "$CUSTOM_GCLOUD_EXE" "$@" > /dev/null 2>&1
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

get_service_json(){
    zone_wo_region=${ZONE%-*}
    f=$CODE_HOME/bu/bucket-$zone_wo_region.json
    
    # if file doesn't exist
    if [ ! -f "$f" ]; then
        echo -e "\033[31m[Internal Error] Service account json file $f not found. Contact admin.\033[0m" >&2
        return 1
    fi
    echo "$f"
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
    elif [[ $ZONE =~ europe-west4.* ]]; then
        echo "gs://kmh-gcp"
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

    local log_file="${ZHH_ENV_CHECK_LOG_FILE:-}"
    py_path=$CONDA_PY_PATH
    # if VM_NAME contains v6, don't use conda
    IS_V6=0
    if use_v6_script $VM_NAME; then
        py_path="python"
        IS_V6=1
    fi

    ENV_CHECK="ls $CODE_HOME > /dev/null && $py_path -c 'import jax, torch; print(jax.__file__)'"
    if [ -n "$log_file" ]; then
        zhh_capture_command_output result "$log_file" timeout 60s "$CUSTOM_GCLOUD_EXE" compute tpus tpu-vm ssh "$VM_NAME" --zone "$ZONE" \
            --worker=all --command "$ENV_CHECK" || true
    else
        result=$(timeout 60s "$CUSTOM_GCLOUD_EXE" compute tpus tpu-vm ssh "$VM_NAME" --zone "$ZONE" \
            --worker=all --command "$ENV_CHECK" 2>&1 || true)
    fi
    # first, eliminate module not found
    if [[ $result == *"ModuleNotFoundError"* ]]; then
        if [ -z "$log_file" ]; then
            echo "Environment is not proper setup: Cannot find torch/jax. Use \`SCRIPT_DEBUG=1\` for more info."
        fi
        return 4
    fi
    if [[ $result == *"No such file"* ]]; then
        if [ -z "$log_file" ]; then
            echo "Environment is not proper setup: Have not mount disk. Use \`SCRIPT_DEBUG=1\` for more info."
        fi
        return 4
    fi

    # if not IS_V6, assert miniforge3 is in result
    if [ ! $IS_V6 -eq 1 ]; then
        if [[ $result =~ *"local"* ]]; then
            if [ -z "$log_file" ]; then
                echo "Wrong python env, expected to in miniforge3. Gonna remove local..."
            fi
            if [ -n "$log_file" ]; then
                export ZHH_CAPTURE_LOG_FILE="$log_file"
            fi
            wrap_gcloud compute tpus tpu-vm ssh "$VM_NAME" --zone "$ZONE" \
                --worker=all --command "sudo rm -rf ~/.local"
            if [ -n "$log_file" ]; then
                unset ZHH_CAPTURE_LOG_FILE
            fi
        fi
    fi

    TEST="sudo rm -rf /tmp/*tpu* && $py_path -c 'import jax; print(jax.devices())'"
    if [ -n "$log_file" ]; then
        zhh_capture_command_output result "$log_file" timeout 120s "$CUSTOM_GCLOUD_EXE" compute tpus tpu-vm ssh "$VM_NAME" --zone "$ZONE" \
            --worker=all --command "$TEST" || true
    else
        result=$(timeout 120s "$CUSTOM_GCLOUD_EXE" compute tpus tpu-vm ssh "$VM_NAME" --zone "$ZONE" \
            --worker=all --command "$TEST" 2>&1 || true)
    fi

    if [[ $result == *"TpuDevice"* ]]; then
        if [ -z "$log_file" ]; then
            echo "Environment setup successful."
        fi
    elif [[ $result == *"jaxlib.xla_extension.XlaRuntimeError: ABORTED: The TPU is already in use by process with pid"* || $result == *"Unable to initialize backend"* ]]; then
        if [ -z "$log_file" ]; then
            echo "TPU is already in use. If you want to persist, use \`zhh k\` and try again."
        fi
        return 3
    elif [[ $result == *"googlecloudsdk.command_lib.util.ssh.ssh.CommandError"* || $result == *"ERROR: (gcloud.compute.tpus.tpu-vm.ssh)"* ]]; then
        if [ -z "$log_file" ]; then
            echo "TPU may be preempted (during environment check!). Gonna re-apply..."
        fi
        return 9
    else
        if [ -z "$log_file" ]; then
            echo "TPU Unkwown Error"
            echo "$result"
        fi
        return 4
    fi
}

while_check_env(){
    # allow user to run "kill" to interrupt
    VM_NAME=$1
    ZONE=$2
    local env_check_log_file=""
    local kill_log_file=""
    local has_pretty_logs=false

    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    if zhh_prepare_log_file env_check_log_file "env_check_trial${ZHH_SETUP_TRIAL:-1}" 2>/dev/null; then
        has_pretty_logs=true
    fi

    if $has_pretty_logs; then
        zhh_step_banner "Check TPU environment" "$env_check_log_file"
        export ZHH_ENV_CHECK_LOG_FILE="$env_check_log_file"
        zhh_step_start_spinner
    else
        echo "[INFO] Checking environment setup..."
    fi

    check_env $VM_NAME $ZONE && ret=0 || ret=$?
    if $has_pretty_logs; then
        unset ZHH_ENV_CHECK_LOG_FILE
    fi
    if [ $ret -eq 0 ]; then
        if $has_pretty_logs; then
            zhh_step_done
        else
            echo "[INFO] Environment is ready."
        fi
        return 0
    fi
    if [ $ret -eq 3 ]; then
        if [ "$ZAK" = "1" ]; then
            if $has_pretty_logs; then
                zhh_step_warn "[BUSY]"
                zhh_note "TPU is busy. Auto-kill is enabled."
            else
                echo "[INFO] Auto-kill is enabled. Attempting to kill the TPU process..."
            fi
            yn="y"
        else
            # read -p "Kill the TPU process right now? (y/n) " yn
            if $has_pretty_logs; then
                zhh_step_warn "[BUSY]"
                zhh_note "TPU is already in use."
            else
                echo -e "TPU is in use. Aborted."
            fi
            yn="n"
        fi
        if [ "$yn" = "y" ]; then
            if zhh_prepare_log_file kill_log_file "kill_tpu_trial${ZHH_SETUP_TRIAL:-1}" 2>/dev/null; then
                zhh_step_banner "Kill TPU process" "$kill_log_file"
                export ZHH_KILL_TPU_LOG_FILE="$kill_log_file"
                zhh_step_start_spinner
            fi
            kill_tpu $VM_NAME $ZONE && ret=0 || ret=$?
            unset ZHH_KILL_TPU_LOG_FILE
            if $has_pretty_logs; then
                if [ $ret -eq 0 ]; then
                    zhh_step_done
                elif [ $ret -eq 9 ]; then
                    zhh_step_warn "[PREEMPTED]"
                else
                    zhh_step_fail "[FAILED]"
                    zhh_note "Log: $kill_log_file"
                fi
            fi
        else
            if $has_pretty_logs; then
                zhh_warn "Not killing the TPU process. See $env_check_log_file"
            else
                echo "[INFO] Not killing the TPU process. Exiting."
            fi
            return 3
        fi
    elif [ $ret -eq 4 ]; then
        if $has_pretty_logs; then
            zhh_step_warn "[RETRY]"
            zhh_note "Environment check failed. Reinstalling runtime."
        else
            echo "[INFO] Environment check failed. Retrying verbose setup..."
        fi
        export SCRIPT_DEBUG=1
        export ZAK=1 # sometimes need to autokill even in the setup phase
        run_setup_script $VM_NAME $ZONE || true
    elif [ $ret -eq 9 ]; then
        if $has_pretty_logs; then
            zhh_step_warn "[PREEMPTED]"
            zhh_note "TPU may be preempted during environment check."
        else
            echo "[INFO] TPU may be preempted. Exiting to re-apply..."
        fi
        return 9
    fi

    if zhh_prepare_log_file env_check_log_file "env_check_retry_trial${ZHH_SETUP_TRIAL:-1}" 2>/dev/null; then
        has_pretty_logs=true
        zhh_step_banner "Re-check TPU environment" "$env_check_log_file"
        export ZHH_ENV_CHECK_LOG_FILE="$env_check_log_file"
        zhh_step_start_spinner
    fi
    check_env $VM_NAME $ZONE && ret=0 || ret=$?
    if [ -n "$ZHH_ENV_CHECK_LOG_FILE" ]; then
        unset ZHH_ENV_CHECK_LOG_FILE
    fi
    if [ $ret -ne 0 ]; then
        if $has_pretty_logs; then
            zhh_step_fail "[FAILED]"
            zhh_note "Log: $env_check_log_file"
        else
            echo -e "\033[31m[Error] Environment is not proper setup: failed to init TPU. Use \`SCRIPT_DEBUG=1\` for more info.\033[0m"
        fi
    elif $has_pretty_logs; then
        zhh_step_done
    fi
    return $ret
}

kill_tpu(){
    VM_NAME=$1
    ZONE=$2
    local log_file="${ZHH_KILL_TPU_LOG_FILE:-}"
    local output=""

    if [ -z "$log_file" ]; then
        echo -e "\033[1m[INFO] killing tpu vm $VM_NAME in $ZONE...\033[0m"
    fi

    if [ -z "$VM_NAME" ]; then
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    # KILLER=$(cat $ZHH_SCRIPT_ROOT/scripts/new_killer.sh)

    sleep 2
    CMD="
    sudo bash $ZHH_SCRIPT_ROOT/scripts/new_killer.sh
    echo job killed
    "
    if [ -n "$log_file" ]; then
        zhh_run_logged_command "$log_file" "$CUSTOM_GCLOUD_EXE" compute tpus tpu-vm ssh "$VM_NAME" --zone="$ZONE" --worker=all --command "$CMD" && ret=0 || ret=$?
    else
        "$CUSTOM_GCLOUD_EXE" compute tpus tpu-vm ssh "$VM_NAME" --zone="$ZONE" --worker=all --command "$CMD" && ret=0 || ret=$?
    fi
    if [ $ret -ne 0 ]; then
        if [ -z "$log_file" ]; then
            echo -e "\033[31m[Error] Failed to kill TPU process. Retrying...\033[0m"
            output=$("$CUSTOM_GCLOUD_EXE" compute tpus tpu-vm ssh "$VM_NAME" --zone="$ZONE" --worker=all --command "$CMD" 2>&1 || true)
            echo "[DEBUG] kill tpu result: $output"
        else
            zhh_capture_command_output output "$log_file" "$CUSTOM_GCLOUD_EXE" compute tpus tpu-vm ssh "$VM_NAME" --zone="$ZONE" --worker=all --command "$CMD" || true
        fi
        if [[ $output == *"[/usr/bin/ssh] exited with return code [255]"* || $output == *"ERROR: (gcloud.compute.tpus.tpu-vm.ssh)"* ]]; then
            if [ -z "$log_file" ]; then
                echo "TPU may be preempted (during killing!). Gonna re-apply..."
            fi
            return 9
        else
            return 1
        fi
    fi
    return $ret
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
    local mount_log_file=""
    local install_log_file=""
    local has_pretty_logs=false

    if zhh_prepare_log_file mount_log_file "mount_disk_trial${ZHH_SETUP_TRIAL:-1}" 2>/dev/null; then
        has_pretty_logs=true
    else
        echo "[INFO] setting up tpu vm $VM_NAME in $ZONE..."
    fi

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
        json_file=$(get_service_json)
        hash_tag=$(cat /dev/urandom | tr -dc 'a-z0-9' | fold -w 6 | head -n 1)
        if use_v5_env $VM_NAME; then
            PIP_INSTALL_STR="

            sudo snap refresh google-cloud-cli || true
            pip uninstall pyOpenSSL cryptography -y || true
            pip install pyOpenSSL cryptography || true

            cd
            gcloud auth activate-service-account --key-file=$json_file

            # kill all existing gsutil processes
            ps -ef | grep gsutil | grep cp | grep -v $hash_tag | grep -v grep | awk '{ print \" sudo kill -9 \" \$2 }' | sh || true
            ps -ef | grep gsutil || true

            /snap/bin/gsutil -m cp -r $gs_str/hanhong/v5_wheels_new.tar.gz ./wheels.tar.gz
            tar -xvf wheels.tar.gz
            rm -rf .local || true
            pip install --no-index --find-links=wheels wheels/*.whl --no-deps --force-reinstall --no-warn-script-location
            rm -rf wheels wheels.tar.gz
            "
        else
            PIP_INSTALL_STR="

            cd
            gcloud auth activate-service-account --key-file=$json_file

            # kill all existing gsutil processes
            ps -ef | grep gsutil | grep cp | grep -v $hash_tag | grep -v grep | awk '{ print \" sudo kill -9 \" \$2 }' | sh || true
            ps -ef | grep gsutil || true

            /snap/bin/gsutil -m cp -r $gs_str/hanhong/v6_wheels_jax437.tar.gz ./wheels.tar.gz
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

    # CMD="
    # $MOUNT_DISK_STR
    # $PIP_INSTALL_STR
    # "
    
    # CMD is sequential print of quoted MOUNT_DISK_STR and PIP_INSTALL_STR
    # CMD=$(printf "%s\n%s" "$MOUNT_DISK_STR" "$PIP_INSTALL_STR")

    if $has_pretty_logs; then
        zhh_step_banner "Mount shared disk" "$mount_log_file"
        export ZHH_CAPTURE_LOG_FILE="$mount_log_file"
        zhh_step_start_spinner
    fi
    wrap_gcloud compute tpus tpu-vm ssh "$VM_NAME" --zone "$ZONE" \
    --worker=all --command "$MOUNT_DISK_STR" && ret=0 || ret=$?
    if $has_pretty_logs; then
        unset ZHH_CAPTURE_LOG_FILE
    fi
    if [ $ret -ne 0 ]; then
        if $has_pretty_logs; then
            zhh_step_fail "[FAILED]"
            zhh_note "Log: $mount_log_file"
        else
            echo -e "\033[31m[Error] Mount disk setup failed. Use \`SCRIPT_DEBUG=1\` for more info.\033[0m"
        fi
        # check if DO_TPU_SETUP is not set
        # if [ "$DO_TPU_SETUP" != "1" ]; then
        #     echo -e "\033[33m[Hint] Is the TPU set up? Use \`DO_TPU_SETUP=1\` to force environment setup on TPU VM.\033[0m"
        # fi
        return 1
    fi
    if $has_pretty_logs; then
        zhh_step_done
        zhh_prepare_log_file install_log_file "install_runtime_trial${ZHH_SETUP_TRIAL:-1}"
        zhh_step_banner "Install TPU runtime" "$install_log_file"
        export ZHH_CAPTURE_LOG_FILE="$install_log_file"
        zhh_step_start_spinner
    fi

    wrap_gcloud compute tpus tpu-vm ssh "$VM_NAME" --zone "$ZONE" \
    --worker=all --command "$PIP_INSTALL_STR" && ret=0 || ret=$?
    if $has_pretty_logs; then
        unset ZHH_CAPTURE_LOG_FILE
    fi
    if [ $ret -ne 0 ]; then
        if $has_pretty_logs; then
            zhh_step_fail "[FAILED]"
            zhh_note "Log: $install_log_file"
        else
            echo -e "\033[31m[Error] Pip install failed. Use \`SCRIPT_DEBUG=1\` for more info.\033[0m"
        fi
        return 1
    fi
    if $has_pretty_logs; then
        zhh_step_done
    fi
}

run_wandb_login(){
    VM_NAME=$1
    ZONE=$2
    local log_file=""
    local output=""
    local has_pretty_logs=false

    if zhh_prepare_log_file log_file "wandb_login_trial${ZHH_SETUP_TRIAL:-1}" 2>/dev/null; then
        has_pretty_logs=true
        zhh_step_banner "Wandb login" "$log_file"
        export ZHH_CAPTURE_LOG_FILE="$log_file"
        zhh_step_start_spinner
    else
        echo "[INFO] wandb login into $VM_NAME in $ZONE..."
    fi

    if [ -z "$VM_NAME" ]; then
        if $has_pretty_logs; then
            zhh_step_fail "[FAILED]"
        fi
        echo -e $VM_UNFOUND_ERROR
        return 1
    fi

    if [ -z "$WANDB_API_KEY" ]; then
        if $has_pretty_logs; then
            zhh_step_fail "[FAILED]"
            zhh_note "Log: $log_file"
        fi
        echo -e "\033[31m[Error] WANDB_API_KEY is not set, so you cannot perform wandb login. Please run \`source .ka\`.\033[0m" >&2
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

    wrap_gcloud compute tpus tpu-vm ssh "$VM_NAME" --zone "$ZONE" \
    --worker=all --command "$CMD" && ret=0 || ret=$?
    if $has_pretty_logs; then
        unset ZHH_CAPTURE_LOG_FILE
    fi
    if [ $ret -ne 0 ]; then
        if $has_pretty_logs; then
            zhh_step_warn "[RETRY]"
            zhh_note "Wandb login failed. Inspecting retry details."
        else
            echo -e "\033[31m[Error] Wandb login failed. Retrying...\033[0m"
        fi
        export SCRIPT_DEBUG=1
        if $has_pretty_logs; then
            zhh_capture_command_output output "$log_file" "$CUSTOM_GCLOUD_EXE" compute tpus tpu-vm ssh "$VM_NAME" --zone "$ZONE" \
                --worker=all --command "$CMD" || true
        else
            output=$(wrap_gcloud compute tpus tpu-vm ssh "$VM_NAME" --zone "$ZONE" \
                --worker=all --command "$CMD" 2>&1 || true)
            echo "[DEBUG] wandb login result: $output"
        fi
        if [[ $output == *"[/usr/bin/ssh] exited with return code [255]"* || $output == *"ERROR: (gcloud.compute.tpus.tpu-vm.ssh)"* ]]; then
            if $has_pretty_logs; then
                zhh_note "Log: $log_file"
                zhh_warn "TPU may be preempted during wandb login."
            else
                echo "TPU may be preempted (during environment check!). Gonna re-apply..."
            fi
            return 9
        else
            if $has_pretty_logs; then
                zhh_note "Log: $log_file"
                zhh_warn "Wandb login failed."
            fi
            return 1
        fi
    fi
    if $has_pretty_logs; then
        zhh_step_done
    fi
}
