set -euo pipefail

FIFO=./myfifo
[[ -p $FIFO ]] || mkfifo $FIFO

role=${1:-}

if [[ $role == "A" ]]; then
    # echo "[A] 通知 B 开始任务"
    # echo "start" > $FIFO

    # echo "[A] 等待 B 完成"

    exec 3<>"$FIFO"
    printf 'start\n' >&3
    # 关闭写端/读端
    exec 3>&- 3<&-

elif [[ $role == "B" ]]; then
    echo "[B] 等待 A 的通知"
    read msg < $FIFO
    echo "[B] 收到 A 的消息: $msg"

    echo "[B] 开始执行工作..."
    sleep 3   # 模拟任务

    echo "[B] 通知 A 已完成"
    # echo "done" > $FIFO

else
    echo "用法: $0 [A|B]"
    exit 1
fi
