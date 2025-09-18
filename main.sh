#!/bin/bash

set -e

if [ ! -z "$SCRIPT_DEBUG" ]; then
    set -x
fi

export ZHH_SCRIPT_ROOT=$(realpath "$(dirname "${BASH_SOURCE[0]}")")
source $ZHH_SCRIPT_ROOT/scripts/launch.sh

# ka.sh has to be sourced in each TMUX window
# source ka.sh

if [ "$1" = "s" ]; then
    # for status, no need to check config sanity
    zstatus
    exit 0
fi

if check_config_sanity; then
    if [ "$1" = "rr" ]; then
        zrerun
    elif [ "$1" = "k" ]; then
        zkill
    elif [ "$1" = "q" ]; then
        zqueue "${@:2}"
    elif [ "$1" = "qq" ]; then
        zqueue_pop
    elif [ "$1" = "w" ]; then
        zwhat
    else
        zrun "$@"
    fi
fi