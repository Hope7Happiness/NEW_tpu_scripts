# until ls /kmh-nfs-ssd-us-mount/code/siri works, repeat
sudo rm -rf /tmp/* || true
sudo rm -rf /tmp/*tpu* || true
sudo rm -rf /tmp/wandb || true


MAX_RETRIES=5
NFS_SERVER="10.97.81.98:/kmh_nfs_ssd_us"
MOUNT_POINT="/kmh-nfs-ssd-us-mount"
TARGET_DIR="$MOUNT_POINT/code/siri"

sudo mkdir -p "$MOUNT_POINT"

########################################
# Stage 1: ensure nfs-common installed
########################################
attempt=1
until sudo apt-get update && sudo apt-get install -y nfs-common; do
    echo "[apt] attempt $attempt/$MAX_RETRIES failed"
    if [ "$attempt" -ge "$MAX_RETRIES" ]; then
        echo "[apt] failed after $MAX_RETRIES attempts"
        exit 1
    fi

    sleep 5
    # stop unattended-upgrades to avoid conflicts
    sudo systemctl stop unattended-upgrades.service || true
    sudo systemctl disable unattended-upgrades.service || true
    # kill all unattended-upgrade processes
    ps -ef | grep -i unattended | grep -v 'grep' | awk '{print "sudo kill -9 " $2}' | sh
    ps -ef | grep -i apt-get | grep -v 'grep' | awk '{print "sudo kill -9 " $2}' | sh

    sudo systemctl stop unattended-upgrades.service || true
    sudo systemctl disable unattended-upgrades.service || true

    attempt=$((attempt + 1))
done

echo "[apt] nfs-common ready"

########################################
# Stage 2: wait until siri dir exists
########################################
attempt=1
until [ -d "$TARGET_DIR" ]; do
    echo "[nfs] attempt $attempt/$MAX_RETRIES"

    timeout 60 sudo mount -o vers=3 "$NFS_SERVER" "$MOUNT_POINT" || true
    sudo chmod go+rw "$MOUNT_POINT" || true

    if [ -d "$TARGET_DIR" ]; then
        break
    fi

    if [ "$attempt" -ge "$MAX_RETRIES" ]; then
        echo "[nfs] $TARGET_DIR not available after $MAX_RETRIES attempts"
        exit 1
    fi

    attempt=$((attempt + 1))
    sleep 5
done

echo "[nfs] $TARGET_DIR is ready"

echo "Mount disk success"
