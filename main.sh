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
    [[ "$1" == "s" || "$1" == "wall" || ("$1" == "w" && "$2" == "all") || "$1" == "dall" || ("$1" == "d" && "$2" == "all") || ("$1" =~ ^h) ]] \
    && echo true || echo false
)
need_concrete_card=$(
    [[ "$1" == "k" || "$1" == "q" || "$1" == "qq" || "$1" == "qrr" ]] \
    && echo true || echo false
)

if $need_concrete_card; then
    if [[ "$VM_NAME" == "*auto*" ]]; then
        echo "Error: command \`zhh $1\` requires a concrete VM_NAME (not 'auto'). Please set VM_NAME to a specific TPU name." >&2
        exit 1
    fi
fi

if $no_need_check || check_config_sanity; then
    if [ "$1" = "rr" ]; then
        zrerun
    elif [ "$1" = "k" ]; then
        zkill
    elif [ "$1" = "q" ]; then
        zqueue "${@:2}"
    elif [ "$1" = "qq" ]; then
        zqueue_pop
    elif [ "$1" = "qrr" ]; then
        zqueue_rerun
    elif [ "$1" = "s" ]; then
        zstatus
    elif [ "$1" = "w" ]; then
        zwhat $2
    elif [ "$1" = "wall" ]; then
        zwhat all
    elif [ "$1" = "d" ]; then
        zdelete
    elif [ "$1" = "dall" ]; then
        zdelete all
    elif [ "$1" = "g" ]; then
        zget
    elif [ "$1" = "mm" ]; then
        run_matmul
    elif [[ "$1" =~ ^h ]]; then
        if command -v pygmentize &> /dev/null; then
            LESSOPEN="| pygmentize -l markdown -O style=vim %s" less -R +/Usage $ZHH_SCRIPT_ROOT/README.md
        else
            less +/Usage $ZHH_SCRIPT_ROOT/README.md
        fi
    else
        zrun "$@"
    fi
fi