# New TPU Scripts

## TODO

## Usage

**ENV VARS**:
- `DO_TPU_SETUP`: if set to 1, do tpu setup (skipped by default)
- `SCRIPT_DEBUG`: if set to 1, enable script debugging mode (skipped by default)

Usage:

0. setup `ka.sh`:


    ```bash
    export VM_NAME=kmh-tpuvm-v4-32-spot-kangyang-xxx
    export ZONE=us-central2-b

    export WANDB_API_KEY=API_KEY_HERE
    export PROJECT=PROJECT_NAME_HERE
    ```

**NOTE**: You should `source ka.sh` in every new terminal. **Each terminal should best be only for one TPU**.

1. kill 

    ```bash
    zhh k
    ```

2. run

    ```bash
    zhh YOUR_ARGS
    ```

3. rerun (rerun requires **NO** arguments)

    ```bash
    zhh rr
    ```