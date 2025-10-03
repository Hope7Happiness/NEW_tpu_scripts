# modified from the source TPU-INFO package, but changed to bash

tpu_from_device(){
    case "$1" in
        0x005e) echo "V4" ;;

        0x0063) echo "V5E" ;;

        0x0062) echo "V5P" ;;

        0x006f) echo "V6E" ;;

        *) return 1 ;;
    esac
}

# translate to bash
get_local_chips() {
    for pci_path in /sys/bus/pci/devices/*; do
        vendor_path="$pci_path/vendor"
        vendor_id=$(cat "$vendor_path" | tr -d '[:space:]')
        if [[ "$vendor_id" != "0x1ae0" ]]; then
            continue
        fi
        device_id_path="$pci_path/device"
        device_id=$(cat "$device_id_path" | tr -d '[:space:]')
        subsystem_path="$pci_path/subsystem_device"
        subsystem_id=$(cat "$subsystem_path" | tr -d '[:space:]')

        chip_type=$(tpu_from_device "$device_id")
        if [[ -n "$chip_type" ]]; then
            echo "$chip_type"
            return 0
        fi
    done
    return 1
}

get_tpu_users(){
    shopt -s nullglob
    for proc in /proc/*; do
        if [[ ! -d "$proc" ]]; then
            continue
        fi
        pid=$(basename "$proc")
        if ! [[ "$pid" =~ ^[0-9]+$ ]]; then
            continue
        fi
        for fd in "$proc"/fd/*; do
            if [[ ! -L "$fd" ]]; then
                continue
            fi
            file=$(readlink "$fd" 2>/dev/null) || continue
            if [[ "$file" =~ ^/dev/accel[0-9]+$ || "$file" =~ ^/dev/vfio/[0-9]+$ ]]; then
                # PIDs+=("$pid")
                # echo "sudo kill -9 $pid" | sh
                sudo kill -9 "$pid"
                break
            fi
        done
    done
    shopt -u nullglob
}

get_tpu_users