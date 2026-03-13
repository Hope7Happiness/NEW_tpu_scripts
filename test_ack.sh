#!/bin/bash

echo "Starting test_ack.sh"
echo "ZHH_JOB_ID=$ZHH_JOB_ID"
echo "ZHH_SERVER_PORT=$ZHH_SERVER_PORT"

cd /kmh-nfs-ssd-us-mount/code/siri/scripts
source .ka

echo "About to run main.sh"
bash main.sh h

echo "main.sh finished with exit code: $?"
echo "Test complete"
