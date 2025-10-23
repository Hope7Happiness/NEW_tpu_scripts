level=0

# stop unattended-upgrades to avoid conflicts
sudo systemctl stop unattended-upgrades.service || true
sudo systemctl disable unattended-upgrades.service || true
# kill all unattended-upgrade processes
ps -ef | grep -i unattended | grep -v 'grep' | awk '{print "sudo kill -9 " $2}'
ps -ef | grep -i unattended | grep -v 'grep' | awk '{print "sudo kill -9 " $2}' | sh

sudo systemctl stop unattended-upgrades.service || true
sudo systemctl disable unattended-upgrades.service || true

while true; do 
    sudo umount -l /kmh-nfs-ssd-eu-mount || true
    ls /kmh-nfs-ssd-us-mount/code/siri
    if [ $? -eq 0 ]; then
        echo "Disk is already mounted."
        break
    fi

    level=$((level+1))
    
    if [ $level -gt 1 ]; then 

        # more advanced mount op
        ps -ef | grep -i unattended | grep -v 'grep' | awk '{print "sudo kill -9 " $2}'
        ps -ef | grep -i unattended | grep -v 'grep' | awk '{print "sudo kill -9 " $2}' | sh
        ps -ef | grep -i unattended | grep -v 'grep' | awk '{print "sudo kill -9 " $2}' | sh
        sleep 5
        sudo apt-get -y update
        sudo apt-get -y install nfs-common
        ps -ef | grep -i unattended | grep -v 'grep' | awk '{print "sudo kill -9 " $2}'
        ps -ef | grep -i unattended | grep -v 'grep' | awk '{print "sudo kill -9 " $2}' | sh
        ps -ef | grep -i unattended | grep -v 'grep' | awk '{print "sudo kill -9 " $2}' | sh
        sleep 6

        for i in {1..10}; do echo Mount Mount 妈妈; done
        sleep 7
        if [ $level -gt 3 ]; then
            echo -e "\033[31m[Error] Disk mount failed after multiple attempts. Please try again.\033[0m"
            exit 1
        fi
    fi

    sudo mkdir -p /kmh-nfs-ssd-us-mount
    sudo mount -o vers=3 10.97.81.98:/kmh_nfs_ssd_us /kmh-nfs-ssd-us-mount
    sudo chmod go+rw /kmh-nfs-ssd-us-mount
    ls /kmh-nfs-ssd-us-mount

    sudo mkdir -p /kmh-nfs-us-mount
	sudo mount -o vers=3 10.26.72.146:/kmh_nfs_us /kmh-nfs-us-mount
	sudo chmod go+rw /kmh-nfs-us-mount
	ls /kmh-nfs-us-mount

done;