"""Agent-facing English prompt strings (edit here for debugging).

Used by: routes/agent.py, runtime/agent_action_protocol.py, runtime/acp_runtime.py.

**CLI session / ``--resume``:** ``routes/agent.py`` builds stdin via ``compose_cli_turn_prompt``.
Protocol details live in the **wecode-server** Claude Code skill (``.claude/skills/wecode-server/``).
Each composed turn ends with a Chinese **nonce footer**; tag JSON must use those nonces.

We do **not** inject a local ``[Conversation Context]`` block; history lives in the Claude Code
session once ``cursor_session_id`` is set and ``acp_prompt_session`` passes ``--resume``.
"""

from __future__ import annotations

# --- New workspace: first CLI stdin when POST /api/conversations creates a session (before any user message) ---
# Edit the body below. Sent once per new conversation (with nonce footer appended by the server).
WORKSPACE_CREATE_GENERAL_PROMPT = """\
You are a agent in a command-line coding assistant session called WeCode. In this environment, beside the normal
Claude tools, there is a concept called JOBS. The JOBS are running in a remote server, but you can interact with them
through a special skill called **wecode-server**. You can launch a job, check its status, and read its logs. Check this
skill very carefully.

You are going to help the user with TPU+JAX coding tasks. The user is in an GCP(Google Cloud Platform) environment, so
jobs are running REMOTELY. When you need to run code, you must launch a job with the **wecode-server** skill instead of running code directly.
Moreover, you may not be an expert with JAX+TPU, so check your TPU/Gemini/JAX/GS bucket skills when you are unsure.

Now, take a look at the repository. Take a look of the core files. README may be out of date, so check the code and comments carefully. If
not otherwise specified, the main code entry is `main.py`, and the main TPU training loop is in `train.py`. The configs are stored in `configs/`. When you use the **wecode-server** skill to launch a job, you can specify the config yaml path in `configs/` and the server will use that config to launch the job.

Good luck!
"""


# --- acp_runtime: mode prefix on the composed prompt (before skills / memory) ---

MODE_ASK_PREFIX = "[Run mode: ask]\nRespond concisely and focus on direct answers.\n\n"
MODE_PLAN_PREFIX = "[Run mode: plan]\nProvide a concrete implementation plan before coding.\n\n"


def apply_mode_prefix(text: str, mode: str) -> str:
    """Prefix user/agent prompt for ask/plan modes; agent mode returns text unchanged."""
    normalized_mode = str(mode or "agent").strip().lower()
    base = str(text or "")
    if normalized_mode == "ask":
        return MODE_ASK_PREFIX + base
    if normalized_mode == "plan":
        return MODE_PLAN_PREFIX + base
    return base


# --- POST /messages: after session_job execution, injected as a synthetic user turn + next CLI prompt ---

SESSION_JOB_FOLLOWUP_USER_REQUEST = (
    "The latest **user** message in context contains **session job** data injected by the server "
    "(it starts with \"[Server · session job results]\"). Read it and respond naturally—summarize, "
    "answer the user's earlier question, or suggest next steps."
)


# --- Task refs / auto-fix: never paste log stdout; model uses session_job query + file tools ---

TASK_REF_JOB_IDS_HEADER = (
    "The following job id(s) are in scope for this turn (no log excerpts attached)."
)

TASK_REF_JOB_IDS_FOOTER = (
    "Use the **wecode-server** skill: **session_job** op \"query\" on each job_id above. "
    "The query result includes **config_path** and **log_file**—inspect both; then read "
    "**log_file** with your file tools."
)


def session_job_followup_core_content(inj_markdown: str) -> str:
    """Stdin body for a resumed CLI session after session_job: job data + short instruction (not in CLI history yet)."""
    body = str(inj_markdown or "").rstrip()
    return f"{body}\n\n{SESSION_JOB_FOLLOWUP_USER_REQUEST}"


# --- session_job parse errors: synthetic user turn before one automatic repair CLI round ---

SESSION_JOB_PARSE_ERROR_AUTOFIX_USER_REQUEST = (
    "Reply with a **valid** `<session_job>...</session_job>` block: JSON object, correct `op` and fields, "
    "and `nonce` exactly equal to the **session_job** value in the Chinese footer at the end of this message. "
    "Fix every issue listed above."
)


def session_job_parse_error_autofix_core_content(formatted_parse_errors_message: str) -> str:
    """Body for the synthetic user message + repair stdin (server appends nonce footer via compose_cli_turn_prompt)."""
    body = str(formatted_parse_errors_message or "").rstrip()
    return f"{body}\n\n{SESSION_JOB_PARSE_ERROR_AUTOFIX_USER_REQUEST}"


def append_server_nonce_footer(
    text: str,
    *,
    session_nonce: str,
    give_up_job_id: str | None = None,
    give_up_nonce: str | None = None,
) -> str:
    """Append Chinese nonce lines to every server-composed CLI user turn (matches wecode-server SKILL.md)."""
    body = str(text or "").rstrip()
    sn = str(session_nonce or "").strip()
    lines = [
        "",
        "---",
        "这条回复的 nonce（每轮不同；`<session_job>` 内 JSON 的 nonce 须与下述一致）：",
        f"- session_job：`{sn}`",
    ]
    gj = str(give_up_job_id or "").strip()
    gn = str(give_up_nonce or "").strip()
    if gj and gn:
        lines.append(
            f"- give_up_fix：`{gn}`（仅当本回合放弃自动修复时使用；标签内 job_id 须为 `{gj}`）"
        )
    lines.append("")
    return body + "\n" + "\n".join(lines)


def auto_fix_trigger_text(job_id: str, status: str, _give_up_nonce: str) -> str:
    """Short stdin + UI user line when a job fails; give-up nonce is only in ``append_server_nonce_footer``."""
    jid = str(job_id or "").strip()
    normalized_status = str(status or "").strip().lower() or "unknown"
    return (
        f"Task `{jid}` finished with status `{normalized_status}`. "
        "Follow the **wecode-server** skill (failed-job / auto-fix section). "
        "Nonces for tags are at the end of this message."
    )
