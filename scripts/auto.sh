# experimental: auto select tpu
source $ZHH_SCRIPT_ROOT/scripts/sscript.sh

POOL_V6=(us-east5-b us-central1-b asia-northeast1-b)
POOL_V5=(us-central1-a us-east5-a)
POOL_V4=(us-central2-b)

auto_select(){
    # if 'auto' in VM_NAME
    if [[ $VM_NAME == "autov6" || $VM_NAME == "auto" ]]; then
        pool=("${POOL_V6[@]}")
        tpu_cls=v6e
    elif [[ "$VM_NAME" =~ "autov5" ]]; then
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

    if [ ! -z "$ZONE" ]; then
        # split $ZONE by comma
        IFS=',' read -r -a pool <<< "$ZONE"
    fi

    if [ -z "$TPU_TYPES" ]; then
        TPU_TYPES="32,64"
    fi

    echo "Auto-selecting zone from pool: ${pool[@]}"
    found_tpu=false
    infos=$(get_available_tpu_infos)
    # todo: low card first
    while read -r vm_name zone; do
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

    if $found_tpu; then
        trap 'echo -e "\n[INFO] Exiting. run this line to set VM_NAME and ZONE: ka $VM_NAME $ZONE;"' EXIT
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
        echo "No suitable zone found"
        return 1
    fi
    # gonna apply for the smallest type in TPU_TYPES
    smallest_type=$(echo $TPU_TYPES | tr ',' '\n' | sort -n | head -n1)
    echo "Applying for TPU VM of type v$tpu_cls-$smallest_type in zone $ZONE"

    # generate a random 6 digit hex code
    rand_hex=$(openssl rand -hex 3)
    export VM_NAME="kmh-tpuvm-$tpu_cls-${smallest_type}-$TPU_DEFAULT_NAME-$rand_hex"
    # tmux
    if [ ! -z "$TMUX" ]; then
        tmux rename-window -t "$TMUX_PANE" $(echo $VM_NAME | sed -E 's/^kmh-tpuvm-v([0-9])[a-z]*-([0-9]+)[a-z-]*-([0-9a-z]+)$/\1-\2-\3/')
    fi
    starting_command
    trap 'echo -e "\n[INFO] Exiting. run this line to set VM_NAME and ZONE: ka $VM_NAME $ZONE;"' EXIT
}