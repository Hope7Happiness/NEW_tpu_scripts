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
    File "/kmh-nfs-ssd-us-mount/code/siri/google-cloud-sdk/lib/googlecloudsdk/
    command_lib/util/ssh/ssh.py", line 1986, in Run
        raise CommandError(args[0], return_code=status)
    googlecloudsdk.command_lib.util.ssh.ssh.CommandError: [/usr/bin/ssh] exited
    with return code [255].
    [Error] Job failed. Check logs in /kmh-nfs-ssd-us-mount/staging/siri/.../output.log
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
    [Info] Queued job /kmh-nfs-ssd-us-mount/staging/siri/...
    /launch_20251102_183108_gitd0c7f12_0df5780d
    at 20251102_183118. Now, the program will stuck, which is EXPECTED. If you want to 
    dequeue, just press Ctrl+C.
    ```

## 🔨 Setting Up

- Setup [common.sh](/scripts/common.sh). 
- Setup `./secret.json`. It should be in the format
    ```json
    {
        "sender": "YOUR GMAIL ADDRESS",
        "receivers": ["reciever1@gmail.com", "reciever2@gmail.com"],
        "password": "YOUR GMAIL APP PASSWORD"
    }
    ```
- Setup your `.bashrc`:
    ```bash
    alias zhh="/kmh-nfs-ssd-us-mount/code/siri/scripts/main.sh"
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

### 🪄 Commands

#### 🔥 What you will use the most

1. **kill**

    ```bash
    zhh k
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