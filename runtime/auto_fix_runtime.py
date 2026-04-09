from __future__ import annotations

import threading
import time
from typing import Any, Callable

from core.tasks.session_job_tools import execute_session_job_actions, format_session_jobs_user_message_content
from runtime.agent_action_protocol import (
    build_auto_fix_prompt,
    extract_give_up_fix_action,
    extract_session_job_actions,
    format_session_job_parse_errors_message,
    new_action_nonce,
)
from runtime.agent_prompt_compose import compose_cli_turn_prompt
from runtime.agent_prompts import (
    append_server_nonce_footer,
    session_job_followup_core_content,
    session_job_parse_error_autofix_core_content,
)
from runtime.tasks_runtime import build_prompt_with_task_refs
from core.global_agent_model import get_global_cli_model, get_global_llm_provider


class AutoFixCoordinator:
    def __init__(
        self,
        *,
        get_conversation: Callable[[str], dict | None],
        get_conversation_lock: Callable[[str], threading.Lock],
        update_conversation: Callable[[str, Callable[[dict], None]], dict],
        append_message: Callable[[str, str, str, dict | None], object],
        resolve_job_status: Callable[[dict, str], str],
        is_failed_task_status: Callable[[str], bool],
        normalize_task_status: Callable[[str | None], str],
        maybe_autoname: Callable[[str], None],
        acp_prompt_session: Callable[..., dict],
        agent_path_getter: Callable[[], str],
        utc_now: Callable[[], float],
        report_agent_event: Callable[[str, dict], None] | None = None,
    ):
        self.get_conversation = get_conversation
        self.get_conversation_lock = get_conversation_lock
        self.update_conversation = update_conversation
        self.append_message = append_message
        self.resolve_job_status = resolve_job_status
        self.is_failed_task_status = is_failed_task_status
        self.normalize_task_status = normalize_task_status
        self.maybe_autoname = maybe_autoname
        self.acp_prompt_session = acp_prompt_session
        self.agent_path_getter = agent_path_getter
        self.utc_now = utc_now
        self.report_agent_event = report_agent_event

        self._store_lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._cancelers: dict[str, Any] = {}
        self._active_job_ids: dict[str, str] = {}

    def request_stop(self, conversation_id: str) -> tuple[bool, str | None]:
        canceler = None
        job_id = None
        with self._store_lock:
            thread = self._threads.get(conversation_id)
            if not (thread and thread.is_alive()):
                return False, None
            event = self._cancel_events.get(conversation_id)
            if event is not None:
                event.set()
            canceler = self._cancelers.get(conversation_id)
            job_id = self._active_job_ids.get(conversation_id)
        if canceler is not None:
            try:
                canceler.close()
            except Exception:
                pass
        return True, job_id

    def _set_task_auto_fix_state(
        self,
        conversation_id: str,
        job_id: str,
        *,
        in_progress: bool | None = None,
        attempted: bool | None = None,
        gave_up_reason: str | None = None,
    ) -> None:
        def updater(c: dict):
            task_meta = c.setdefault("task_meta", {})
            entry = task_meta.get(job_id)
            if not isinstance(entry, dict):
                entry = {}
            else:
                entry = dict(entry)

            if in_progress is not None:
                entry["auto_fix_in_progress"] = bool(in_progress)
                entry["auto_fix_updated_at"] = self.utc_now()
                if in_progress:
                    entry.pop("auto_fix_pending", None)
                    entry.pop("auto_fix_pending_at", None)
                    entry["unread"] = False
                    entry["alert_kind"] = ""
                    c["status"] = "debugging"
                elif str(c.get("status") or "").strip().lower() == "debugging":
                    c["status"] = "idle"
            if attempted is not None:
                entry["auto_fix_attempted"] = bool(attempted)
                entry["auto_fix_updated_at"] = self.utc_now()
                if attempted:
                    entry.pop("auto_fix_pending", None)
                    entry.pop("auto_fix_pending_at", None)
            if gave_up_reason is not None:
                if str(gave_up_reason).strip():
                    entry["auto_fix_gave_up_reason"] = str(gave_up_reason).strip()
                else:
                    entry.pop("auto_fix_gave_up_reason", None)
                entry["auto_fix_updated_at"] = self.utc_now()
            task_meta[job_id] = entry

        self.update_conversation(conversation_id, updater)

    def _run_worker(self, conversation_id: str, job_id: str) -> None:
        cancel_event = threading.Event()
        with self._store_lock:
            self._cancel_events[conversation_id] = cancel_event
            self._active_job_ids[conversation_id] = job_id

        try:
            self._set_task_auto_fix_state(conversation_id, job_id, in_progress=True)

            if cancel_event.is_set():
                raise InterruptedError("auto-fix canceled")

            conv = self.get_conversation(conversation_id)
            if not conv:
                self.append_message(
                    conversation_id,
                    "system",
                    f"Auto-fix skipped for job {job_id}: conversation not found.",
                    {"system_event": "auto_fix_skipped", "job_id": job_id, "reason": "conversation_not_found"},
                )
                return

            lock = self.get_conversation_lock(conversation_id)
            acquired = False
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if cancel_event.is_set():
                    raise InterruptedError("auto-fix canceled")
                acquired = lock.acquire(timeout=0.5)
                if acquired:
                    break
            if not acquired:
                self.append_message(
                    conversation_id,
                    "system",
                    f"Auto-fix skipped for job {job_id}: conversation busy.",
                    {"system_event": "auto_fix_skipped", "job_id": job_id},
                )
                return

            try:
                latest = self.get_conversation(conversation_id)
                if not latest:
                    self.append_message(
                        conversation_id,
                        "system",
                        f"Auto-fix skipped for job {job_id}: conversation disappeared.",
                        {"system_event": "auto_fix_skipped", "job_id": job_id, "reason": "conversation_disappeared"},
                    )
                    return

                if cancel_event.is_set():
                    raise InterruptedError("auto-fix canceled")

                status = self.resolve_job_status(latest, job_id)
                if not self.is_failed_task_status(status):
                    self.append_message(
                        conversation_id,
                        "system",
                        f"Auto-fix skipped for job {job_id}: current status is {status}.",
                        {
                            "system_event": "auto_fix_skipped",
                            "job_id": job_id,
                            "reason": "status_not_failed",
                            "job_status": str(status),
                        },
                    )
                    return

                give_up_nonce = new_action_nonce()
                session_job_nonce = new_action_nonce()
                auto_fix_instruction = build_auto_fix_prompt(job_id, status, give_up_nonce)

                prompt_text = append_server_nonce_footer(
                    build_prompt_with_task_refs(auto_fix_instruction, [{"job_id": job_id}]),
                    session_nonce=session_job_nonce,
                    give_up_job_id=job_id,
                    give_up_nonce=give_up_nonce,
                )

                if cancel_event.is_set():
                    raise InterruptedError("auto-fix canceled")

                self.append_message(
                    conversation_id,
                    "user",
                    auto_fix_instruction,
                    {
                        "task_refs": [job_id],
                        "task_ref_sources": {job_id: "session_job query"},
                        "auto_fix": True,
                    },
                )

                reporter = self.report_agent_event

                def _apply_acp_result_to_conv(res: dict) -> None:
                    snap = self.get_conversation(conversation_id) or latest
                    model_used = str(res.get("model") or "").strip()
                    effort_used = str(res.get("effort") or snap.get("llm_effort") or "").strip().lower()
                    context_tokens = res.get("context_tokens")
                    context_window = res.get("context_window")

                    def set_runtime_metadata(c: dict):
                        if not c.get("cursor_session_id"):
                            c["cursor_session_id"] = res["cursor_session_id"]
                        c["llm_model"] = get_global_cli_model()
                        if c.get("llm_effort"):
                            c["llm_effort"] = str(c.get("llm_effort")).strip().lower()
                        else:
                            c["llm_effort"] = "high"
                        if model_used:
                            c["current_model"] = model_used
                        if effort_used:
                            c["current_effort"] = effort_used
                        if isinstance(context_tokens, int) and context_tokens >= 0:
                            c["current_context_tokens"] = context_tokens
                        if isinstance(context_window, int) and context_window > 0:
                            c["current_context_window"] = context_window
                        if c.get("memory_summary_pending"):
                            c["memory_summary_pending"] = False

                    self.update_conversation(conversation_id, set_runtime_metadata)

                def _parse_after_session_tags(after_sj: str) -> tuple[str, str | None]:
                    return extract_give_up_fix_action(after_sj, job_id, give_up_nonce)

                result = self.acp_prompt_session(
                    agent_path=self.agent_path_getter(),
                    cwd=latest["cwd"],
                    mode=latest.get("mode", "agent"),
                    text=prompt_text,
                    cursor_session_id=latest.get("cursor_session_id"),
                    preferred_model=get_global_cli_model(),
                    llm_provider=get_global_llm_provider(),
                    effort=str(latest.get("llm_effort") or "high").strip().lower() or "high",
                    on_progress_event=(
                        (lambda event: reporter(conversation_id, event))
                        if reporter is not None
                        else None
                    ),
                    cancel_event=cancel_event,
                    on_client_ready=lambda canceler: self._register_canceler(conversation_id, canceler),
                )
                self._clear_canceler(conversation_id)

                _apply_acp_result_to_conv(result)

                assistant_raw = result["text"] or f"[No text returned; stopReason={result['stop_reason']}]"
                after_sj1, sa1, sj_parse_errors = extract_session_job_actions(
                    assistant_raw, session_job_nonce,
                )
                after_sj = after_sj1
                if sj_parse_errors:
                    repair_body = session_job_parse_error_autofix_core_content(
                        format_session_job_parse_errors_message(sj_parse_errors),
                    )
                    repair_nonce = new_action_nonce()
                    repair_prompt = append_server_nonce_footer(
                        build_prompt_with_task_refs(repair_body, [{"job_id": job_id}]),
                        session_nonce=repair_nonce,
                        give_up_job_id=job_id,
                        give_up_nonce=give_up_nonce,
                    )
                    self.append_message(
                        conversation_id,
                        "user",
                        repair_body,
                        {
                            "session_job_parse_error_autofix": True,
                            "errors": sj_parse_errors,
                            "auto_fix": True,
                        },
                    )
                    latest = self.get_conversation(conversation_id) or latest
                    if cancel_event.is_set():
                        raise InterruptedError("auto-fix canceled")
                    result = self.acp_prompt_session(
                        agent_path=self.agent_path_getter(),
                        cwd=latest["cwd"],
                        mode=latest.get("mode", "agent"),
                        text=repair_prompt,
                        cursor_session_id=latest.get("cursor_session_id"),
                        preferred_model=get_global_cli_model(),
                        llm_provider=get_global_llm_provider(),
                        effort=str(latest.get("llm_effort") or "high").strip().lower() or "high",
                        on_progress_event=(
                            (lambda event: reporter(conversation_id, event))
                            if reporter is not None
                            else None
                        ),
                        cancel_event=cancel_event,
                        on_client_ready=lambda canceler: self._register_canceler(conversation_id, canceler),
                    )
                    self._clear_canceler(conversation_id)
                    _apply_acp_result_to_conv(result)
                    assistant_raw = result["text"] or f"[No text returned; stopReason={result['stop_reason']}]"
                    after_sj, sa2, sj_parse_errors2 = extract_session_job_actions(
                        assistant_raw, repair_nonce,
                    )
                    if sj_parse_errors2:
                        self.append_message(
                            conversation_id,
                            "system",
                            format_session_job_parse_errors_message(sj_parse_errors2),
                            {"system_event": "session_job_parse_error", "errors": sj_parse_errors2, "auto_fix": True},
                        )
                else:
                    sa2 = []

                assistant_text, gave_up_reason = _parse_after_session_tags(after_sj)
                tuples1, _ = execute_session_job_actions(conversation_id, sa1)
                if sj_parse_errors:
                    tuples2, _ = execute_session_job_actions(conversation_id, sa2)
                    tuples = tuples1 + tuples2
                else:
                    tuples = tuples1

                if tuples:
                    inj_body = format_session_jobs_user_message_content(tuples)
                    results_meta = [{"op": o, "payload": p} for o, p in tuples]
                    self.append_message(
                        conversation_id,
                        "user",
                        inj_body,
                        {
                            "session_job_injection": True,
                            "session_job_results": results_meta,
                            "auto_fix": True,
                        },
                    )
                    latest2 = self.get_conversation(conversation_id)
                    if latest2 and not cancel_event.is_set():
                        session_job_nonce = new_action_nonce()
                        p_follow = compose_cli_turn_prompt(
                            session_job_followup_core_content(inj_body),
                            session_job_nonce,
                            [],
                        )
                        result = self.acp_prompt_session(
                            agent_path=self.agent_path_getter(),
                            cwd=latest2["cwd"],
                            mode=latest2.get("mode", "agent"),
                            text=p_follow,
                            cursor_session_id=latest2.get("cursor_session_id"),
                            preferred_model=get_global_cli_model(),
                            llm_provider=get_global_llm_provider(),
                            effort=str(latest2.get("llm_effort") or "high").strip().lower() or "high",
                            on_progress_event=(
                                (lambda event: reporter(conversation_id, event))
                                if reporter is not None
                                else None
                            ),
                            cancel_event=cancel_event,
                            on_client_ready=lambda canceler: self._register_canceler(conversation_id, canceler),
                        )
                        self._clear_canceler(conversation_id)
                        _apply_acp_result_to_conv(result)
                        assistant_raw = result["text"] or f"[No text returned; stopReason={result['stop_reason']}]"
                        after_sj2, sa2, sj_err2 = extract_session_job_actions(assistant_raw, session_job_nonce)
                        if sj_err2:
                            self.append_message(
                                conversation_id,
                                "system",
                                format_session_job_parse_errors_message(sj_err2),
                                {"system_event": "session_job_parse_error", "errors": sj_err2, "auto_fix": True},
                            )
                        assistant_text, gave_up_reason = _parse_after_session_tags(after_sj2)
                        execute_session_job_actions(conversation_id, sa2)

                if assistant_text.strip():
                    self.append_message(conversation_id, "assistant", assistant_text, {"auto_fix": True})

                if gave_up_reason:
                    self._set_task_auto_fix_state(conversation_id, job_id, gave_up_reason=gave_up_reason)
                    self.append_message(
                        conversation_id,
                        "system",
                        f"error: give up to fix job {job_id}. Reason: {gave_up_reason}",
                        {
                            "system_event": "auto_fix_give_up",
                            "job_id": job_id,
                            "reason": gave_up_reason,
                        },
                    )

                self.maybe_autoname(conversation_id)
            finally:
                lock.release()
        except InterruptedError:
            self.append_message(
                conversation_id,
                "system",
                f"Auto-fix stopped by user for job {job_id}.",
                {"system_event": "auto_fix_stopped", "job_id": job_id},
            )
        except Exception as e:
            self.append_message(
                conversation_id,
                "system",
                f"Auto-fix failed for job {job_id}: {e}",
                {"system_event": "auto_fix_error", "job_id": job_id},
            )
        finally:
            try:
                self._clear_canceler(conversation_id)
                self._set_task_auto_fix_state(conversation_id, job_id, in_progress=False, attempted=True)
            finally:
                with self._store_lock:
                    self._threads.pop(conversation_id, None)
                    self._cancel_events.pop(conversation_id, None)
                    self._cancelers.pop(conversation_id, None)
                    self._active_job_ids.pop(conversation_id, None)

    def _register_canceler(self, conversation_id: str, canceler: Any) -> None:
        with self._store_lock:
            self._cancelers[conversation_id] = canceler

    def _clear_canceler(self, conversation_id: str) -> None:
        with self._store_lock:
            self._cancelers.pop(conversation_id, None)

    def maybe_schedule(self, conversation_id: str, conv: dict, jobs: list[dict]) -> None:
        if bool(conv.get("auto_iterating")):
            return

        with self._store_lock:
            existing = self._threads.get(conversation_id)
            if existing and existing.is_alive():
                return

        task_meta = conv.get("task_meta")
        if not isinstance(task_meta, dict):
            return

        failed_job_id = None
        for job in (jobs or []):
            if not isinstance(job, dict):
                continue
            job_id = str(job.get("job_id") or "").strip()
            if not job_id:
                continue
            status = self.normalize_task_status(str(job.get("status") or ""))
            if not self.is_failed_task_status(status):
                continue
            entry = task_meta.get(job_id)
            if not isinstance(entry, dict):
                entry = {}
            if not bool(entry.get("auto_fix_pending", False)):
                continue
            if bool(entry.get("auto_fix_in_progress")):
                continue
            if bool(entry.get("auto_fix_attempted")):
                continue
            if str(entry.get("auto_fix_gave_up_reason") or "").strip():
                continue
            failed_job_id = job_id
            break

        if not failed_job_id:
            return

        self._set_task_auto_fix_state(conversation_id, failed_job_id, in_progress=True, attempted=True)
        self.append_message(
            conversation_id,
            "system",
            f"Auto-fix started for failed job {failed_job_id}.",
            {"system_event": "auto_fix_start", "job_id": failed_job_id},
        )

        thread = threading.Thread(
            target=self._run_worker,
            args=(conversation_id, failed_job_id),
            daemon=True,
            name=f"auto-fix-{conversation_id[:8]}",
        )
        with self._store_lock:
            self._threads[conversation_id] = thread
        thread.start()
