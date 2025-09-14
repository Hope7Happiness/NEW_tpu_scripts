#!/bin/bash

set -ex
export ZHH_SCRIPT_ROOT=$(realpath "$(dirname "${BASH_SOURCE[0]}")")
# source ka.sh

# ka.sh has to be sourced in each TMUX window

source $ZHH_SCRIPT_ROOT/scripts/launch.sh

if [ "$1" = "rr" ]; then
    zrerun
elif [ "$1" = "k" ]; then
    zkill
else
    zrun "$@"
fi