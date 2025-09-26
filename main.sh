#!/bin/bash

set -e

if [ ! -z "$SCRIPT_DEBUG" ]; then
    set -x
fi

export ZHH_SCRIPT_ROOT=$(realpath "$(dirname "${BASH_SOURCE[0]}")")
source $ZHH_SCRIPT_ROOT/scripts/launch.sh

# ka.sh has to be sourced in each TMUX window
# source ka.sh
no_need_check=$(
    [[ "$1" == "s" || "$1" == "wall" || ("$1" == "w" && "$2" == "all") ]] \
    && echo true || echo false
)

if $no_need_check || check_config_sanity; then
    if [ "$1" = "rr" ]; then
        zrerun
    elif [ "$1" = "k" ]; then
        zkill
    elif [ "$1" = "q" ]; then
        zqueue "${@:2}"
    elif [ "$1" = "qq" ]; then
        zqueue_pop
    elif [ "$1" = "s" ]; then
        zstatus
    elif [ "$1" = "w" ]; then
        zwhat $2
    elif [ "$1" = "wall" ]; then
        zwhat all
    elif [ "$1" = "g" ]; then
        zget
    else
        zrun "$@"
    fi
fi