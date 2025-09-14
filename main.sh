set -ex

# source ka.sh

# ka.sh has to be sourced in each TMUX window

source scripts/launch.sh

if [ "$1" = "rr" ]; then
    zrerun
elif [ "$1" = "k" ]; then
    zkill
else
    zrun "$@"
fi