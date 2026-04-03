from __future__ import annotations

import threading
import time
from typing import Any, Callable

from agent_action_protocol import (
    build_auto_fix_prompt,
    extract_give_up_fix_action,
    extract_run_job_action,
    new_action_nonce,
    with_run_job_skill_instruction,
)
from tasks_runtime import build_prompt_with_task_refs


class AutoFixCoordinator:
    def __init__(
        self,
        *,
        get_conversation: Callable[[str], dict | None],
        get_conversation_lock: Callable[[str], threading.Lock],
        update_conversation: Callable[[str, Callable[[dict], None]], dict],
        append_message: Callable[[str, str, str, dict | None], object],
        build_task_reference_payload: Callable[[str, dict, str], tuple[str, str]],
        resolve_job_status: Callable[[dict, str], str],
        is_failed_task_status: Callable[[str], bool],
        normalize_task_status: Callable[[str | None], str],
        maybe_autoname: Callable[[str], None],
        acp_prompt_session: Callable[..., dict],
        agent_path_getter: Callable[[], str],
        trigger_run_job: Callable[[str, bool], tuple[str | None, str | None]],
        utc_now: Callable[[], float],
        report_agent_event: Callable[[str, dict], None] | None = None,
    ):
        self.get_conversation = get_conversation
        self.get_conversation_lock = get_conversation_lock
        self.update_conversation = update_conversation
        self.append_message = append_message
        self.build_task_reference_payload = build_task_reference_payload
        self.resolve_job_status = resolve_job_status
        self.is_failed_task_status = is_failed_task_status
        self.normalize_task_status = normalize_task_status
        self.maybe_autoname = maybe_autoname
        self.acp_prompt_session = acp_prompt_session
        self.agent_path_getter = agent_path_getter
        self.trigger_run_job = trigger_run_job
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

                run_action_nonce = new_action_nonce()
                give_up_nonce = new_action_nonce()
                auto_fix_instruction = build_auto_fix_prompt(job_id, status, give_up_nonce)
                memory = str(latest.get("memory_summary") or "").strip()
                memory_pending = bool(latest.get("memory_summary_pending", False))
                cursor_session_id = str(latest.get("cursor_session_id") or "").strip()
                if memory and (memory_pending or not cursor_session_id):
                    auto_fix_instruction_for_model = (
                        "[Conversation memory summary]\n"
                        f"{memory}\n\n"
                        "[Current user request]\n"
                        f"{auto_fix_instruction}"
                    )
                else:
                    auto_fix_instruction_for_model = auto_fix_instruction

                stdout_text, source = self.build_task_reference_payload(conversation_id, latest, job_id)
                prompt_text = build_prompt_with_task_refs(
                    with_run_job_skill_instruction(auto_fix_instruction_for_model, run_action_nonce),
                    [{"stdout": stdout_text}],
                )

                if cancel_event.is_set():
                    raise InterruptedError("auto-fix canceled")

                self.append_message(
                    conversation_id,
                    "user",
                    auto_fix_instruction,
                    {
                        "task_refs": [job_id],
                        "task_ref_sources": {job_id: source},
                        "auto_fix": True,
                    },
                )

                reporter = self.report_agent_event
                result = self.acp_prompt_session(
                    agent_path=self.agent_path_getter(),
                    cwd=latest["cwd"],
                    mode=latest.get("mode", "agent"),
                    text=prompt_text,
                    cursor_session_id=latest.get("cursor_session_id"),
                    on_progress_event=(
                        (lambda event: reporter(conversation_id, event))
                        if reporter is not None
                        else None
                    ),
                    cancel_event=cancel_event,
                    on_client_ready=lambda canceler: self._register_canceler(conversation_id, canceler),
                )
                self._clear_canceler(conversation_id)

                model_used = str(result.get("model") or "").strip()
                if (not latest.get("cursor_session_id")) or model_used:
                    def set_runtime_metadata(c: dict):
                        if not c.get("cursor_session_id"):
                            c["cursor_session_id"] = result["cursor_session_id"]
                        if model_used:
                            c["current_model"] = model_used
                        if c.get("memory_summary_pending"):
                            c["memory_summary_pending"] = False
                    self.update_conversation(conversation_id, set_runtime_metadata)

                assistant_raw = result["text"] or f"[No text returned; stopReason={result['stop_reason']}]"
                assistant_cleaned, should_run_job = extract_run_job_action(assistant_raw, run_action_nonce)
                assistant_text, gave_up_reason = extract_give_up_fix_action(
                    assistant_cleaned,
                    job_id,
                    give_up_nonce,
                )

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

                should_auto_start_run = (not should_run_job) and (not gave_up_reason)
                if should_run_job or should_auto_start_run:
                    new_job_id, run_err = self.trigger_run_job(conversation_id, True)
                    if new_job_id:
                        auto_fix_tag = f"auto fix from {job_id}"
                        def apply_auto_fix_tag(c: dict):
                            task_meta = c.setdefault("task_meta", {})
                            entry = task_meta.get(new_job_id)
                            if not isinstance(entry, dict):
                                entry = {}
                            else:
                                entry = dict(entry)
                            entry["nickname"] = auto_fix_tag
                            entry["auto_fix_from_job_id"] = job_id
                            entry["updated_at"] = self.utc_now()
                            task_meta[new_job_id] = entry
                        self.update_conversation(conversation_id, apply_auto_fix_tag)

                        if should_run_job:
                            self.append_message(
                                conversation_id,
                                "system",
                                "action: run job",
                                {"system_event": "agent_action_run_job"},
                            )
                    elif run_err:
                        if should_run_job:
                            err_msg = f"Agent requested run job, but /run failed: {run_err}"
                        else:
                            err_msg = f"Auto-fix attempted automatic run job, but /run failed: {run_err}"
                        self.append_message(
                            conversation_id,
                            "system",
                            err_msg,
                            {"system_event": "task_run_failed", "auto_run_by_agent": True},
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
