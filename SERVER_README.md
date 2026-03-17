# ZHH Job Server 使用文档

轻量级 HTTP server，用于管理 zhh 任务的远程执行。

## 启动 Server

```bash
# 默认端口 8080
python server.py

# 自定义端口和主机
python server.py --port 8081 --host 0.0.0.0

# 后台运行
python server.py &
```

## API 接口

### 1. 创建任务 (POST /run)

在新的 tmux session 中启动 zhh 任务。

**请求：**
```bash
curl -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{
    "cwd": "/path/to/working/directory",
    "args": "zhh参数（可选）"
  }'
```

**参数：**
- `cwd` (必填): 工作目录路径
- `args` (可选): 传递给 zhh 的参数，如 "rr", "q", "s" 等

**响应示例：**
```json
{
  "job_id": "4f771738-a452-4bab-98c3-39be046c7215",
  "status": "running",
  "tmux_session": "zhh_4f771738",
  "cwd": "/kmh-nfs-ssd-us-mount/code/siri/scripts",
  "zhh_args": "s",
  "created_at": "2026-03-12T15:11:44.650747",
  "updated_at": "2026-03-12T15:11:44.650757"
}
```

**示例：**
```bash
# 运行 zhh s（查看状态）
curl -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"cwd": "/kmh-nfs-ssd-us-mount/code/siri/pixel_jit", "args": "s"}'

# 运行训练任务
curl -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"cwd": "/kmh-nfs-ssd-us-mount/code/siri/pixel_jit"}'
```

---

### 2. 查看所有任务 (GET /status)

**请求：**
```bash
curl http://localhost:8080/status
```

**响应示例：**
```json
{
  "count": 2,
  "jobs": [
    {
      "job_id": "4f771738-a452-4bab-98c3-39be046c7215",
      "status": "completed",
      "exit_code": 0,
      "tmux_session": "zhh_4f771738",
      "cwd": "/kmh-nfs-ssd-us-mount/code/siri/scripts",
      "zhh_args": "s",
      "created_at": "2026-03-12T15:11:44.650747",
      "completed_at": "2026-03-12T15:11:45.592879"
    },
    {
      "job_id": "8381d1e0-d39e-4188-b91c-93611f1ed484",
      "status": "running",
      "tmux_session": "zhh_8381d1e0",
      "cwd": "/kmh-nfs-ssd-us-mount/code/siri/pixel_jit",
      "zhh_args": "",
      "created_at": "2026-03-12T03:53:47.607846"
    }
  ]
}
```

---

### 3. 查看单个任务 (GET /status/<job_id>)

**请求：**
```bash
curl http://localhost:8080/status/4f771738-a452-4bab-98c3-39be046c7215
```

**响应示例：**
```json
{
  "job_id": "4f771738-a452-4bab-98c3-39be046c7215",
  "status": "completed",
  "exit_code": 0,
  "tmux_session": "zhh_4f771738",
  "cwd": "/kmh-nfs-ssd-us-mount/code/siri/scripts",
  "zhh_args": "s",
  "created_at": "2026-03-12T15:11:44.650747",
  "completed_at": "2026-03-12T15:11:45.592879"
}
```

---

### 4. 取消任务 (POST /cancel/<job_id>)

先向任务对应的 tmux pane 发送 `Ctrl+C`，再关闭 tmux window，并从任务列表中删除。

**请求：**
```bash
curl -X POST http://localhost:8080/cancel/4f771738-a452-4bab-98c3-39be046c7215
```

**响应示例：**
```json
{
  "job_id": "4f771738-a452-4bab-98c3-39be046c7215",
  "status": "cancelled",
  "message": "Job cancelled and removed"
}
```

---

### 5. 查看任务日志 (GET /log/<job_id>)

统一日志接口：
- **任务运行中**：实时从 tmux pane 抓取日志（不落盘）
- **任务已结束**：返回退出时保存的最终日志文件内容

**请求：**
```bash
curl "http://localhost:8080/log/4f771738-a452-4bab-98c3-39be046c7215?lines=2000"
```

**参数：**
- `lines` (可选): 最多回溯的行数，默认 `2000`

**运行中响应示例（source=tmux）：**
```json
{
  "job_id": "4f771738-a452-4bab-98c3-39be046c7215",
  "status": "running",
  "tmux_session": "zhh_4f771738",
  "lines": 2000,
  "source": "tmux",
  "log": "...实时输出..."
}
```

**结束后响应示例（source=file）：**
```json
{
  "job_id": "4f771738-a452-4bab-98c3-39be046c7215",
  "status": "completed",
  "tmux_session": "zhh_4f771738",
  "lines": 2000,
  "source": "file",
  "log_file": "/kmh-nfs-ssd-us-mount/code/siri/scripts/logs/4f771738-a452-4bab-98c3-39be046c7215.log",
  "log": "...最终日志..."
}
```

---

### 6. 恢复任务 (POST /resume)

根据历史日志路径恢复任务，执行逻辑为：
- 从 `log_path`（如 `.../output.log`）取父目录
- 再 `cd ../..` 到 `logs` 的父目录
- 在该目录执行 `source .ka` 后运行 `${SCRIPT_ROOT}/main.sh rr`

**请求：**
```bash
curl -X POST http://localhost:8080/resume \
  -H "Content-Type: application/json" \
  -d '{
    "log_path": "/kmh-nfs-ssd-us-mount/staging/.../logs/log1_xxx/output.log"
  }'
```

也支持直接传日志目录：
```bash
curl -X POST http://localhost:8080/resume \
  -H "Content-Type: application/json" \
  -d '{
    "log_dir": "/kmh-nfs-ssd-us-mount/staging/.../logs/log1_xxx"
  }'
```

**参数：**
- `log_path` (可选): 历史输出日志文件路径（例如 `output.log`）
- `log_dir` (可选): 历史日志目录路径（与 `log_path` 二选一）

**响应示例：**
```json
{
  "job_id": "9f6ac5e3-4ac3-4f95-a5c2-7bb16d2f8484",
  "status": "running",
  "mode": "resume",
  "command": "/kmh-nfs-ssd-us-mount/code/siri/scripts/main.sh rr",
  "cwd": "/kmh-nfs-ssd-us-mount/staging/chris_t2i/t2i/launch_xxx",
  "tmux_session": "zhh_9f6ac5e3"
}
```

---

### 7. 健康检查 (GET /health)

**请求：**
```bash
curl http://localhost:8080/health
```

**响应示例：**
```json
{
  "status": "ok",
  "timestamp": "2026-03-12T15:11:44.650747"
}
```

---

## 任务状态说明

- `starting`: 任务正在启动
- `running`: 任务正在运行
- `completed`: 任务成功完成
- `failed`: 任务执行失败
- `cancelled`: 任务被取消

---

## 查看任务输出

推荐用 HTTP 接口直接看日志：

```bash
# 运行中：实时抓取
curl "http://localhost:8080/log/<job_id>?lines=2000"

# 结束后：自动返回归档日志内容
curl "http://localhost:8080/log/<job_id>?lines=2000"
```

任务运行在独立的 tmux session 中，也可以 attach 查看实时输出：

```bash
# 方式 1: 使用 tmux attach
tmux attach -t zhh_4f771738

# 方式 2: 如果有 tat alias
tat zhh_4f771738

# 查看所有 zhh sessions
tmux ls | grep zhh_

# 快速查看最近的输出（不进入 session）
tmux capture-pane -t zhh_4f771738 -p | tail -50
```

退出 tmux session：按 `Ctrl+B` 然后 `D`（detach，保持运行）

---

## 工作流程

1. **启动 Server**
   ```bash
   python server.py &
   ```

2. **提交任务**
   ```bash
   curl -X POST http://localhost:8080/run \
     -H "Content-Type: application/json" \
     -d '{"cwd": "/path/to/project"}'
   ```

3. **查看状态**
   ```bash
   curl http://localhost:8080/status
   ```

4. **查看输出**
   ```bash
  curl "http://localhost:8080/log/<job_id>?lines=2000"
   ```

5. **取消任务（可选）**
    ```bash
    curl -X POST http://localhost:8080/cancel/<job_id>
    ```

6. **恢复任务（可选）**
   ```bash
   curl -X POST http://localhost:8080/resume \
     -H "Content-Type: application/json" \
     -d '{"log_path": "/path/to/logs/log1_xxx/output.log"}'
   ```

---

## 配置

### 环境变量

- `ZHH_SERVER_PORT`: Server 端口（默认 8080）

### 数据存储

任务状态保存在 `jobs.json` 文件中（与 server.py 同目录）

---

## 注意事项

1. **目录必须存在**: `cwd` 参数指定的目录必须存在，否则返回 400 错误
2. **需要 tmux**: 系统必须安装 tmux
3. **.ka 必须存在**: `cwd` 目录下必须有 `.ka` 文件
4. **resume 路径规则**: `/resume` 会基于 `log_path/log_dir` 推导到 `../..` 目录执行 `main.sh rr`
5. **任务完成后 session 会退出**: 查看输出请使用 `/log/<job_id>`
6. **自动 ack**: 任务完成时会自动更新状态，无需手动操作

---

## 故障排查

### Server 无法启动
```bash
# 检查端口占用
lsof -i :8080

# 杀死旧进程
pkill -f "python.*server.py"
```

### 任务无法创建
```bash
# 检查目录是否存在
ls -la /path/to/working/directory

# 检查 tmux 是否安装
which tmux

# 手动测试 tmux 命令
tmux new-session -d -s test_session "echo hello"
```

### 查看 Server 日志
```bash
# 前台运行查看输出
python server.py

# 查看后台进程输出
ps aux | grep "python.*server.py"
```
