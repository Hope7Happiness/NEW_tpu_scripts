set -euo pipefail

FIFO=./myfifo
[[ -p $FIFO ]] || mkfifo $FIFO

role=${1:-}

if [[ $role == "A" ]]; then
    # exec 3>"$FIFO"
    # printf 'start\n' >&3
    # # 关闭写端/读端
    # exec 3>&- # 3<&-
    printf 'start\n' >"$FIFO"

elif [[ $role == "B" ]]; then
    # exec 3<>"$FIFO"
    echo "[B] 等待 A 的通知"
    # exec 3<"$FIFO"
    # IFS= read -r msg <&3
    read -r msg <"$FIFO"
    echo "[B] 收到 A 的消息: $msg"

    echo "[B] 开始执行工作..."
    sleep 1   # 模拟任务
    echo "[B] 已完成"
else
    echo "用法: $0 [A|B]"
    exit 1
fi
