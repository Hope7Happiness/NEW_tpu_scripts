semail(){
    python3 $ZHH_SCRIPT_ROOT/pemail.py "$@" || echo -e "\033[33m[Warning] Failed to send email.\033[0m"
}

VM_UNFOUND_ERROR="\033[31m[Internal Error] VM_NAME is not set. Contact admin.\033[0m"
