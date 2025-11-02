##### YOUR SETTINGS #####

CONDA_PY_PATH="/kmh-nfs-ssd-us-mount/code/eva/miniforge3/bin/python3" # your conda python path
STAGING_NAME=siri # your stage dir is at /kmh-nfs-ssd-us-mount/staging/<STAGING_NAME>
GS_STAGING_NAME=qiao_zhicheng_hanhong_files # your gs staging dir is at gs://kmh-gcp-us-central2/<GS_STAGING_NAME>
TPU_DEFAULT_NAME=kangyang

##### END OF YOUR SETTINGS #####

# hint: ZHH_SCRIPT_ROOT will be defined in main.sh
semail(){
    python3 $ZHH_SCRIPT_ROOT/pemail.py "$@" || echo -e "\033[33m[Warning] Failed to send email.\033[0m"
}

VM_UNFOUND_ERROR="\033[31m[Internal Error] VM_NAME is not set. Contact admin.\033[0m"
ZONE_UNFOUND_ERROR="\033[31m[Internal Error] ZONE is not set or incorrect. Contact admin.\033[0m"