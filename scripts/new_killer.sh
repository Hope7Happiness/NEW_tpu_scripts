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
            echo "sudo kill -9 $pid"
            sudo kill -9 "$pid"
            break
        fi
    done
done
shopt -u nullglob