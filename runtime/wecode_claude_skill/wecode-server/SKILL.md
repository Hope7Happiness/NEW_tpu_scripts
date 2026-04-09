---
name: wecode-server
description: >-
  WeCode server: remote training jobs only via <session_job> tags (run / list / query) and optional
  <give_up_fix> on auto-fix turns. Per-message session_job nonce in a Chinese footer. Query returns
  config_path and log_file; run requires config_path and description, optional nickname. Use when
  nonce lines appear, or user mentions remote jobs, job ids, task refs, or failed-job auto-fix.
---

# WeCode server

The backend reads **only** the plain text of your **assistant reply**. Put `<session_job>…</session_job>`
(and `<give_up_fix>…</give_up_fix>` when applicable) **in that visible text**, not only in tools or
internal reasoning.

## Critical: tags in the written reply

Tags must appear as **literal substrings** in the user-visible assistant message. If they are not in
that text, the server will not execute the action.

---

## Per-turn nonce (footer)

Each server-composed user message ends with:

```text
---
这条回复的 nonce（每轮不同；`<session_job>` 内 JSON 的 nonce 须与下述一致）：
- session_job：`…`
```

Copy the **`session_job`** value into **every** `<session_job>` tag’s JSON `nonce` field for that turn.

Auto-fix turns may also list **`give_up_fix`**; use that nonce only inside `<give_up_fix>`.

---

## `<session_job>` — only control channel

All remote actions use this tag. **One** JSON object per tag, **one** tag per action in that turn
(unless your workflow needs multiple lines—still each tag is self-contained).

### `op: "query"` — one job’s metadata

Returns (in the next injected message) at least:

| Field | Use |
|-------|-----|
| **config_path** | Workspace-relative YAML used for that job—**open and review** with file tools. |
| **log_file** | Primary log path—**read** for errors. |
| **status** | Job status. |
| **description** | Short label. |

```text
<session_job>{"op":"query","nonce":"PASTE_SESSION_NONCE","job_id":"abc-123"}</session_job>
```

**Example reply:**

```text
Fetching server metadata for job abc-123 (config + log).

<session_job>{"op":"query","nonce":"d4e5f6789012345678abcdef01234567","job_id":"abc-123"}</session_job>
```

### `op: "list"`

```text
<session_job>{"op":"list","nonce":"PASTE_SESSION_NONCE","status":"all"}</session_job>
```

`status` optional: omit for running-like only; `"all"`; or e.g. `"running,failed"`.

### `op: "run"` — start a run from a **specific** YAML

**Required JSON keys:** `op`, `nonce`, **`config_path`**, **`description`** (short label, ≤80 chars).

**Optional:** **`nickname`** — if set, used as the display nickname for the task (≤80 chars); if
omitted, `description` is used for display.

The server copies that YAML into the remote run slot and invokes `/run`. This is the **only** way to
start jobs from the agent—including the default file **`configs/remote_run_config.yml`** when that is
the YAML you intend to run:

```text
<session_job>{"op":"run","nonce":"PASTE_SESSION_NONCE","config_path":"configs/remote_run_config.yml","description":"full default training run"}</session_job>
```

Custom experiment:

```text
<session_job>{"op":"run","nonce":"PASTE_SESSION_NONCE","config_path":"configs/experiment.yml","description":"ablation","nickname":"abl-v2"}</session_job>
```

---

## Task references

When the user message lists job ids without log excerpts, **`query`** each id, then read **`config_path`**
and **`log_file`** from the result.

---

## `<give_up_fix>` — auto-fix only

Only when the server trigger includes a **`give_up_fix`** nonce line. **Mandatory** `reason`; **`job_id`**
and **`nonce`** must match the trigger.

```text
<give_up_fix>{"job_id":"abc-123","reason":"Cannot fix bucket ACL from here.","nonce":"PASTE_NONCE"}</give_up_fix>
```

---

## Quick reference

| Goal | Example |
|------|---------|
| Start any run (incl. default YAML) | `session_job` `run` with `config_path` + `description` [+ `nickname`] |
| Inspect job | `session_job` `query` with `job_id` |
| List jobs | `session_job` `list` |
| Give up auto-fix | `give_up_fix` with matching nonces |
