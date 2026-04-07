#!/bin/bash

echo "Starting test_ack.sh"
echo "ZHH_JOB_ID=$ZHH_JOB_ID"
echo "ZHH_SERVER_PORT=$ZHH_SERVER_PORT"

CURCHAT_USER="${CURCHAT_USER:-${WHO:-$(whoami)}}"
cd "/kmh-nfs-ssd-us-mount/code/${CURCHAT_USER}/scripts"
source .ka

echo "About to run main.sh"
bash main.sh h

echo "main.sh finished with exit code: $?"
echo "Test complete"
