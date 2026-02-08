# until ls /kmh-nfs-ssd-us-mount/code/siri works, repeat
sudo mkdir -p /kmh-nfs-ssd-us-mount
sudo mount -o vers=3 10.97.81.98:/kmh_nfs_ssd_us /kmh-nfs-ssd-us-mount
sudo chmod go+rw /kmh-nfs-ssd-us-mount

until ls /kmh-nfs-ssd-us-mount/code/siri > /dev/null; do
    sleep 5
    # stop unattended-upgrades to avoid conflicts
    sudo systemctl stop unattended-upgrades.service || true
    sudo systemctl disable unattended-upgrades.service || true
    # kill all unattended-upgrade processes
    ps -ef | grep -i unattended | grep -v 'grep' | awk '{print "sudo kill -9 " $2}' | sh

    sudo systemctl stop unattended-upgrades.service || true
    sudo systemctl disable unattended-upgrades.service || true

    sudo rm -rf /tmp/*tpu* || true
    sudo rm -rf /tmp/wandb || true

    sleep 5

    sudo apt-get -y update
    sudo apt-get -y install nfs-common

    sleep 5
    sudo apt-get -y update
    sudo apt-get -y install nfs-common

    sudo mkdir -p /kmh-nfs-ssd-us-mount
    sudo mount -o vers=3 10.97.81.98:/kmh_nfs_ssd_us /kmh-nfs-ssd-us-mount
    sudo chmod go+rw /kmh-nfs-ssd-us-mount
    # ls /kmh-nfs-ssd-us-mount
done