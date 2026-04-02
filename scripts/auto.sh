# experimental: auto select tpu
source $ZHH_SCRIPT_ROOT/scripts/sscript.sh
source $ZHH_SCRIPT_ROOT/scripts/apply.sh

POOL_V6=(us-east5-b us-central1-b asia-northeast1-b)
# POOL_V6=(us-east5-b us-central1-b asia-northeast1-b europe-west4-a)
POOL_V5=(us-central1-a us-east5-a)
POOL_V4=(us-central2-b)

trap_send_key(){
    CMD="echo -e \"\n\033[32m[INFO] Exiting. Remember to run this line: $1 \033[0m\" && [ -z \"$TMUX\" ] || tmux send-keys -t \"$TMUX_PANE\" \"$1\" "
    trap "$CMD" EXIT
}

TOU_RESULT_PATH="/kmh-nfs-ssd-us-mount/code/${WHO:-wxb}/tou_result.txt"

auto_select(){
    # if 'auto' in VM_NAME
    if [[ $VM_NAME == "autov6" || $VM_NAME == "autov6e" || $VM_NAME == "auto" ]]; then
        pool=("${POOL_V6[@]}")
        tpu_cls=v6e
    elif [[ "$VM_NAME" =~ "autov5" || "$VM_NAME" =~ "autov5p" ]]; then
        pool=("${POOL_V5[@]}")
        tpu_cls=v5p
    elif [[ "$VM_NAME" =~ "autov4" ]]; then
        pool=("${POOL_V4[@]}")
        tpu_cls=v4
    elif [[ "$VM_NAME" =~ "auto" ]]; then
        echo -e "\033[31m[WARNING] Unsupported auto selection argument: $VM_NAME. Current support: autov6, auto\033[0m" >&2
        return 1
    else
        return 0
    fi

    ZONE_INITIAL=""
    if [ ! -z "$ZONE" ]; then
        # split $ZONE by comma
        IFS=',' read -r -a pool <<< "$ZONE"
        ZONE_INITIAL=$ZONE
    fi

    if [ -z "$TPU_TYPES" ]; then
        TPU_TYPES="32,64"
    fi

    # this may lead to concurrency bug, so add a lock here
    exec 200>/tmp/ka_auto_lock

    echo "[DEBUG] Waiting for auto lock..."
    flock 200
    echo "[DEBUG] Lock acquired"

    echo "Auto-selecting zone from pool: ${pool[@]}"
    found_tpu=false
    # infos=$(get_available_tpu_infos)
    # infos is empty
    infos=""
    # concat infos with $TOU_RESULT_PATH

    # if NO_TOU=1, skip
    if [ "$NO_TOU" != "1" ]; then
        # if tou_result.txt is later than 30 mins, abort
        if [ -f "$TOU_RESULT_PATH" ]; then
            lmt=$(stat -c %Y "$TOU_RESULT_PATH")
            now=$(date +%s)
            if (( now - lmt > 1800 )); then
                echo -e "\033[31m[ERROR] tou_result.txt is older than 30 mins. Please refresh it.\033[0m" >&2
                return 1
            fi
        fi

        infos="$infos"$'\n'"$(cat "$TOU_RESULT_PATH" 2>/dev/null || true)"
    fi

    # todo: low card first
    while read -r vm_name zone; do

        # ensure $tpu_cls in $vm_name
        if [[ "$vm_name" != *"$tpu_cls"* ]]; then
            continue
        fi

        # echo "vm=$vm_name, zone=$zone"
        # kmh-tpuvm-v6e-32-kangyang-5 -> 32
        # kmh-tpuvm-[a-z0-9]+-([0-9]+)-...
        tpu_type=$(echo $vm_name | grep -oE 'v[0-9a-z]+-[0-9]+' | cut -d'-' -f2)
        # if zone not in pool, skip
        if [[ ! " ${pool[@]} " =~ " ${zone} " ]]; then
            continue
        fi
        # if tpu_type not in TPU_TYPES, skip
        good_use=false
        for t in ${TPU_TYPES//,/ }; do
            if [[ "$tpu_type" == "$t" ]]; then
                good_use=true
                break
            fi
        done
        if ! $good_use; then
            continue
        fi

        # test if tpu is actually ready
        if ! has_tpu $vm_name $zone; then
            echo -e "[INFO] Found not ready TPU VM $vm_name in zone $zone..."
            continue
        fi

        # or, if tpu is registered and in use, skip
        if ! tpu_info_available "$SSCRIPT_HOME/$vm_name"; then
            echo -e "[INFO] Found in-use TPU VM $vm_name in zone $zone..."
            continue
        fi

        # or, if the lock file (shared across group) exists, skip
        group_lock_file="/kmh-nfs-ssd-us-mount/code/qiao/tpu_lock/*_${vm_name}_*"
        # get the actual file
        actual_lock_file=$(ls $group_lock_file 2>/dev/null || true)
        if [ -f "$actual_lock_file" ]; then
            # if the actual lock file exists, and the name isn't zak
            # if [[ ! "$actual_lock_file" =~ "zak" ]]; then
                echo -e "[INFO] Found locked TPU VM $vm_name in zone $zone (lock file: $(basename $actual_lock_file))..."
                # check the name of the lock file: date -u +%Y-%m-%d_%H-%M-%S
                # don't use stat
                lmt=$(date -r "$actual_lock_file" +%s)
                # use UTC
                now=$(date -u +%s)
                # if the lock file is older than 30 mins, consider it stale and ignore
                if (( now - lmt > 1800 )); then
                    echo -e "\033[33m[WARNING] Found stale lock file for TPU VM $vm_name: $(basename $actual_lock_file). Ignoring the lock.\033[0m"
                    # remove the stale lock file
                    # sudo rm -f "$actual_lock_file"
                    continue # debug, this should not happen
                else
                    continue
                fi
            # else
            #     echo -e "[INFO] Found our lock for TPU VM $vm_name in zone $zone (lock file: $(basename $actual_lock_file)). Ignoring the lock.\033[0m"
            # fi
        fi

        echo -e "Found TPU VM: \033[32m$vm_name @ $zone\033[0m (type $tpu_cls-$tpu_type)"
        export VM_NAME=$vm_name
        export ZONE=$zone
        found_tpu=true

        # little help: rename tmux window
        if [ ! -z "$TMUX" ]; then
            tmux rename-window -t "$TMUX_PANE" $(echo $VM_NAME | sed -E 's/^kmh-tpuvm-v([0-9])[a-z]*-([0-9]+)[a-z-]*-([0-9a-z]+)$/\1-\2-\3/')
        fi

        break
    done <<< "$infos"

    # release lock
    flock -u 200

    if $found_tpu; then
        # trap 'echo -e "\n\033[32m[INFO] Exiting. run this line to set VM_NAME and ZONE: ka $VM_NAME $ZONE;"\033[0m' EXIT
        starting_command
        trap_send_key "ka $VM_NAME $ZONE"
        return 0
    fi

    echo "[INFO] No available TPU VM found in the specified pool and types."
    echo "[INFO] Going to apply..."

    # first list all tpus

    best_zone=""
    best_available=-1
    for zone in "${pool[@]}"; do
        available=$(gcloud compute tpus tpu-vm list --zone $zone 2>/dev/null \
        | awk 'NR>1 {
            split($3, a, "-")
            sum += a[2]
        }
        END {
            total = 1536; used = sum; available = total - used
            print available
        }')
        if [[ -z "$available" ]]; then
            echo -e "[Internal Error] Failed to get TPU info for zone $zone"
            return 2
        fi
        if (( available > best_available )); then
            best_available=$available
            best_zone=$zone
        fi
    done

    if [[ -n "$best_zone" ]]; then
        echo "Auto-selected zone: $best_zone with $best_available available TPUs"
        export ZONE=$best_zone
    else
        echo "No suitable zone found, using default zone."
        # use the first item in pool
        export ZONE=${pool[0]}
        echo "Using zone: $ZONE"
    fi
    # gonna apply for the smallest type in TPU_TYPES
    smallest_type=$(echo $TPU_TYPES | tr ',' '\n' | sort -n | head -n1)
    echo "Applying for TPU VM of type $tpu_cls-$smallest_type in zone $ZONE"

    # generate a random 6 digit hex code
    rand_hex=$(openssl rand -hex 3)
    export VM_NAME="kmh-tpuvm-$tpu_cls-${smallest_type}-$TPU_DEFAULT_NAME-$rand_hex"
    # tmux
    if [ ! -z "$TMUX" ]; then
        tmux rename-window -t "$TMUX_PANE" $(echo $VM_NAME | sed -E 's/^kmh-tpuvm-v([0-9])[a-z]*-([0-9]+)[a-z-]*-([0-9a-z]+)$/\1-\2-\3/')
    fi
    starting_command
    # trap 'echo -e "\n\033[32m[INFO] Exiting. run this line to set VM_NAME and ZONE: ka $VM_NAME $ZONE;"\033[0m' EXIT
    trap_send_key "ka $VM_NAME $ZONE"
}
