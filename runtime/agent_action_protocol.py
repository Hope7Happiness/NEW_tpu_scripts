from __future__ import annotations

import json
import re
import uuid


RUN_JOB_ACTION_PATTERN = re.compile(r"<run_job>(.*?)</run_job>", re.IGNORECASE | re.DOTALL)
GIVE_UP_FIX_TAG_PATTERN = re.compile(r"<give_up_fix>(.*?)</give_up_fix>", re.IGNORECASE | re.DOTALL)


def new_action_nonce() -> str:
    return uuid.uuid4().hex


def with_run_job_skill_instruction(base_text: str, run_nonce: str) -> str:
    instruction = (
        "[Server skill: run job]\n"
        "You have an internal skill named \"run job\".\n"
        "Definition: run job == run the current remote_run_config.yaml with no extra args (equivalent to command: run).\n"
        "Critical rule: if user asks to \"run/跑\" the whole project, full pipeline, training, evaluation, or any long experiment, "
        "you MUST trigger run job skill instead of running local shell commands directly.\n"
        "If user asks to run job, do NOT do local test runs unless user explicitly says to run locally.\n"
        "Do NOT execute full-codebase experiment commands locally when run job skill applies.\n"
        "When and only when the user explicitly asks to run/start/launch that job now, append this exact action tag in your final response:\n"
        f"<run_job>{{\"command\":\"run\",\"nonce\":\"{run_nonce}\"}}</run_job>\n"
        "If user did not ask to run the job now, do not output this tag."
    )
    return f"{base_text}\n\n{instruction}"


def extract_run_job_action(text: str, run_nonce: str) -> tuple[str, bool]:
    raw = str(text or "")
    expected_nonce = str(run_nonce or "").strip()
    should_run = False

    def _replace(match: re.Match[str]) -> str:
        nonlocal should_run
        payload = str(match.group(1) or "").strip()
        if not payload:
            return ""

        parsed = None
        try:
            parsed = json.loads(payload)
        except Exception:
            parsed = None

        command = ""
        nonce = ""
        if isinstance(parsed, dict):
            command = str(parsed.get("command") or "").strip().lower()
            nonce = str(parsed.get("nonce") or "").strip()

        if command == "run" and nonce == expected_nonce:
            should_run = True
        return ""

    cleaned = RUN_JOB_ACTION_PATTERN.sub(_replace, raw)
    cleaned = cleaned.strip()
    return cleaned, should_run


def build_auto_fix_prompt(job_id: str, status: str, give_up_nonce: str) -> str:
    target_job_id = str(job_id or "").strip()
    normalized_status = str(status or "").strip().lower() or "unknown"
    nonce = str(give_up_nonce or "").strip()
    return (
        f"Task {target_job_id} finished with status '{normalized_status}'.\n"
        "Please inspect the referenced logs and fix the issue directly in this workspace.\n"
        "After making fixes, explain what you changed and why.\n"
        "In some cases, it may be possible that the issue is just random, if you are sure of that, you can just state the reason without making any modifications.\n"
        "Beside code errors, there are two specific types of errors you might encounter: 1. environmental errors, such as missing packages, which you can fix by adding a 补.sh in the ROOT of the project, the content of it will be executed before the job runs, you can put any shell commands in it to prepare the environment, but notice that most packages (e.g. JAX/flax) are already auto-installed, you only need to add the custom packages you introduce; 2. GS bucket errors, in which you should review your GS bucket skill. Specifically, you are permitted to copy SMALL checkpoints LOCALLY (i.e. run in your shell) if needed.\n"
        "Finally, only if you are truly blocked and cannot safely fix it now, append exactly one give-up tag in your final response:\n"
        f"<give_up_fix>{{\"job_id\":\"{target_job_id}\",\"reason\":\"<specific reason>\",\"nonce\":\"{nonce}\"}}</give_up_fix>\n"
        "Do not use the give-up tag unless absolutely necessary. The reason is mandatory."
    )


def extract_give_up_fix_action(text: str, job_id: str, give_up_nonce: str) -> tuple[str, str | None]:
    raw = str(text or "")
    normalized_job_id = str(job_id or "").strip()
    expected_nonce = str(give_up_nonce or "").strip()
    reason: str | None = None

    def _replace(match: re.Match[str]) -> str:
        nonlocal reason
        payload = str(match.group(1) or "").strip()
        if not payload:
            return ""

        parsed = None
        try:
            parsed = json.loads(payload)
        except Exception:
            parsed = None

        target_job_id = ""
        target_reason = ""
        nonce = ""
        if isinstance(parsed, dict):
            target_job_id = str(parsed.get("job_id") or "").strip()
            target_reason = str(parsed.get("reason") or "").strip()
            nonce = str(parsed.get("nonce") or "").strip()

        if not target_job_id:
            target_job_id = normalized_job_id
        if target_job_id == normalized_job_id and target_reason and nonce == expected_nonce:
            reason = target_reason
        return ""

    cleaned = GIVE_UP_FIX_TAG_PATTERN.sub(_replace, raw)
    cleaned = cleaned.strip()
    return cleaned, reason
