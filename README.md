# WeCode Quick Start

WeCode is a web UI for multi-session Claude Code development + auto experiment iteration.

## 0) Prerequisites

- Python 3.9+
- `tmux`
- `curl`
- Claude Code CLI (`claude`) installed and logged in
- Node.js runtime new enough for Claude Code CLI (old Node like v12 will fail)

## 1) Set runtime identity (required)

WeCode uses `WECODE_USER` as the canonical username for repo/staging paths.

```bash
export WECODE_USER=<your_username>
```

Backward compatibility:
- Existing scripts still read `WHO`; if `WHO` is unset, they fall back to `WECODE_USER`.

## 2) Choose ports and set task server URL

If default ports are occupied, pick your own ports:

```bash
export TASK_PORT=18080
export WECODE_PORT=17860
export WECODE_TASK_SERVER_URL="http://127.0.0.1:${TASK_PORT}"
```

(You can keep using a shell variable named `WECODE_PORT`. The app reads `WECODE_TASK_SERVER_URL`.)

## 3) Start task server (`server.py`)

```bash
python3 server.py --host 127.0.0.1 --port "${TASK_PORT}"
```

## 4) Start WeCode UI server (`cursor_server_refactored.py`)

```bash
python3 cursor_server_refactored.py --host 0.0.0.0 --port "${WECODE_PORT}" --agent-path claude --cwd .
```

Optional config file defaults are in `config.json` (`ui_server.port`, `ui_server.task_server_url`, `ui_server.workdir_root`, `ui_server.default_cwd`, `ui_server.agent_path`).

## 5) Open frontend in browser

Local machine:

```text
http://127.0.0.1:<WECODE_PORT>
```

With the example above:

```text
http://127.0.0.1:17860
```

Notes:
- UI server port: env `CURSOR_SERVER_PORT`, or `WECODE_UI_PORT` / `WECODE_PORT`, or CLI `--port`.
- Task server URL can also be overridden with env `WECODE_TASK_SERVER_URL`.
- For remote devices (phone/tablet), run UI with `--host 0.0.0.0` and use the machine IP/Tailscale IP.

# 🚀 New TPU Scripts

TODO List:

## ⚡️ New feaures

- [X] Auto card selection

    ```shell
    $ VM_NAME=autov4 TPU_TYPES=64 zhh
    Auto-selecting zone from pool: us-central2-b
    [INFO] No available TPU VM found in the specified pool and types.
    [INFO] Going to apply...
    Auto-selected zone: us-central2-b with 928 available TPUs
    Applying for TPU VM of type v4-64 in zone us-central2-b
    [INFO] You are using VM_NAME=kmh-tpuvm-v4-64-kangyang-9745eb (ZONE=us-central2-b)
    [INFO] staging files
    [INFO] Attempt number 1 to get and setup TPU...
    [INFO] requesting tpu vm kmh-tpuvm-v4-64-kangyang-9745eb in us-central2-b...
    ```

- [X] Auto resume from previous job

    ```shell
    Traceback (most recent call last):
    ...
    File "/kmh-nfs-ssd-us-mount/code/<WECODE_USER>/google-cloud-sdk/lib/googlecloudsdk/
    command_lib/util/ssh/ssh.py", line 1986, in Run
        raise CommandError(args[0], return_code=status)
    googlecloudsdk.command_lib.util.ssh.ssh.CommandError: [/usr/bin/ssh] exited
    with return code [255].
    [Error] Job failed. Check logs in /kmh-nfs-ssd-us-mount/staging/<WECODE_USER>/.../output.log
    [Error] Job failed, first wait for a moment (feel free to ^C if you are here)...
    [INFO] Checking TPU status...
    [Info] Card is PREEMPTED, will re-apply and re-run.
    [INFO] Attempt number 1 to get and setup TPU...
    [INFO] requesting tpu vm kmh-tpuvm-v4-64-kangyang-30d6fb in us-central2-b...
    ```

- [X] Job queue support

    ```shell
    $ zhh q --config.logging.wandb_notes='sanity check'
    [INFO] You are using VM_NAME=kmh-tpuvm-v4-64-kangyang-f2dc9b (ZONE=us-central2-b)
    Queue the job on kmh-tpuvm-v4-64-kangyang-f2dc9b, with args
    --config.logging.wandb_notes=sanity check... ? (y/N) y
    [INFO] staging files
    TPU is already in use. If you want to persist, use `zhh k` and try again.
    [Info] Queued job /kmh-nfs-ssd-us-mount/staging/<WECODE_USER>/...
    /launch_20251102_183108_gitd0c7f12_0df5780d
    at 20251102_183118. Now, the program will stuck, which is EXPECTED. If you want to 
    dequeue, just press Ctrl+C.
    ```

## 🔨 Setting Up

- Setup [common.sh](/scripts/common.sh). 
- Setup `./tools/secret.json`. It should be in the format
    ```json
    {
        "sender": "YOUR GMAIL ADDRESS",
        "receivers": ["reciever1@gmail.com", "reciever2@gmail.com"],
        "password": "YOUR GMAIL APP PASSWORD"
    }
    ```
- Setup your `.bashrc`:
    ```bash
    alias zhh="/path/to/this/repo/main.sh"
    alias zzz="zhh s"
    ```

    Here is a good helper for setting environment variables, which you may also want to add into your `.bashrc`:
    ```bash
    ka(){
        if [ -z "$1" ]; then
            if [[ ! -z "$VM_NAME" ]]; then
            echo $VM_NAME
        else
            echo "no tpu selected"
        fi

        else
            export VM_NAME=$1
            if [ ! -z "$2" ]; then export ZONE=$2; fi
            if [ ! -z "$TMUX" ]; then tmux rename-window $(echo $VM_NAME | sed -E 's/^kmh-tpuvm-v([0-9])[a-z]*-([0-9]+)[a-z-]*-([0-9a-z]+)$/\1-\2-\3/'); fi
        fi
    }
    ```

## Usage

### Centralized TPU Center MVP

The centralized path is intentionally separate from the legacy decentralized `zhh` workflow.

Current MVP behavior:
- `zhh submit` stages the current workspace and writes a durable request into `/kmh-nfs-ssd-us-mount/staging/.tpu_center/inbox`.
- `zhh center start` ingests inbox requests, discovers candidate TPUs from `itou`, and starts matching runs in detached tmux workers.
- `zhh center s` shows centralized runs.
- `zhh center cancel <run_id>` cancels a run and kills its assigned TPU by default.
- This first slice uses existing TPUs from `itou`; it does **not** create new TPU VMs yet.

Submit a run:

```bash
VM_NAME=autov5p TPU_TYPES=64 ZONE=us-east5-a zhh submit --priority 100
```

Pass extra training args through to the future worker:

```bash
zhh submit --priority 100 -- --config.foo=bar
```

Run the center loop:

```bash
zhh center start
```

Show centralized status:

```bash
zhh center s
```

Show the center TPU inventory:

```bash
zhh center tpus
```

Cancel a run:

```bash
zhh center cancel <run_id>
```

### 📄 Requirements

**Environment vars**: You must have the following environment variables set:
- `VM_NAME`: the name of your TPU VM
    - `autovx` will automatically select a free TPU VM of type `x`. In this case `ZONE` can be left unset. `autov5` will default to `v5p` instead of `v5e`. `auto` is equivalent to `autov6`.
- `ZONE`: the zone of your TPU VM
    - In `auto` mode, if your code can't support all tpus in the given type, use a comma-separated list to specify the zones to select from, e.g., `us-central1-b,us-east5-b`.
- `WANDB_API_KEY`: your wandb api key
- `PROJECT` (not forced, but suggested): your project name, used to create staging directory. If unset, default to `unknown`.

Optional env vars:
- `ZAK` (**Z**HH **A**uto-**K**ill): if set to 1, auto-kill the TPU process when setup fails (unset by default)
- `TPU_TYPES`: a comma-separated (but unordered) list of TPU types to select from when using `auto` mode (default to `32,64`).
- `SCRIPT_DEBUG`: if set to 1, enable script debugging mode (unset by default)
- `TPU_IS_NEW`: this is a trick to fool the script to force re-setup the TPU environment, if set to 1 (unset by default)
- `FAST_DEBUG`: if set to 1, skip the TPU get and setup process (unset by default). Useful for debugging code with fast iterations.

### 🪄 Commands

#### 🔥 What you will use the most

1. **kill**

    ```bash
    zhh k
    ```

    Kill a specific TPU without setting environment variables:

    ```bash
    zhh kill <vm_name> <zone>
    ```

2. run (`YOUR_ARGS` are the arguments passed into the `main.py` program)

    ```bash
    zhh YOUR_ARGS
    ```

3. show all job status

    ```bash
    zzz
    ```

4. queue a job when all cards are in use (if runable, then directly run; otherwise stuck until runable)

    ```bash
    zhh q YOUR_ARGS
    ```

    **Note**: to queue, you must specify a **concrete** `VM_NAME` (i.e., no `auto` allowed).

#### 🛠️ Other useful commands

1. show TPU status: this shows the status of the current `VM_NAME`.

    ```bash
    zhh w
    ```

    To show all TPU status, use `zhh w all` or `zhh wall`.

2. rerun (rerun requires **NO** arguments, but **REQUIRES** to be run at the **STAGING** directory)

    ```bash
    zhh rr
    ```

3. manually release a queue slot (submit the first element in queue to running)

    Hint: usually you shouldn't manually run this

   ```bash
    zhh qq
    ```

4. deregister a tpu

    Hint: The TPU must not be in `ready` status in order to deregister it. To refresh the status, use `zhh w` first.

    ```bash
    zhh d
    ```

    To deregister all bad tpus, use `zhh dall`.

5. Run matmul to keep the TPU alive (only support V6 tpus)

    ```bash
    zhh mm
    ```
