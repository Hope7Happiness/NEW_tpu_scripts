# New TPU Scripts

## Usage

0. setup `ka.sh`:


    ```bash
    export VM_NAME=kmh-tpuvm-v4-32-spot-kangyang-xxx
    export ZONE=us-central2-b

    export WANDB_API_KEY=API_KEY_HERE
    export PROJECT=PROJECT_NAME_HERE
    ```

**NOTE**: You should `source ka.sh` in every new terminal. **Each terminal should best be only for one TPU**.

**ENV vars**:
- `DO_TPU_SETUP`: if set to 1, do tpu setup (skipped by default)
- `SCRIPT_DEBUG`: if set to 1, enable script debugging mode (skipped by default)
- `ZAK` (ZHH auto-kill): if set to 1, auto-kill the TPU process when setup fails (unset by default)

**Main Usages**:

1. kill 

    ```bash
    zhh k
    ```

2. run (`YOUR_ARGS` are the arguments passed into the `main.py` program)

    ```bash
    zhh YOUR_ARGS
    ```

3. rerun (rerun requires **NO** arguments, but **REQUIRES** to be run at the **STAGING** directory)

    ```bash
    zhh rr
    ```

4. show all job status

    ```bash
    zzz
    ```

5. show TPU status: this shows the status of the current `VM_NAME`.

    ```bash
    zhh w
    ```

    To show all TPU status, use `zhh w all` or `zhh wall`.

6. queue a job (if runable, then directly run; otherwise stuck until runable)

    ```bash
    zhh q YOUR_ARGS
    ```

7. release a queue slot (submit the first element in queue to running)

    NOTE: usually you shouldn't manually run this

   ```bash
    zhh qq
   ```

8. Run matmul to keep the TPU alive (only support V6 tpus)

   ```bash
   zhh mm
   ```