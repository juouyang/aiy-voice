#!/usr/bin/env python3
"""Small, dependency-free OpenCode HTTP client for AIY Voice sessions.

Only opaque OpenCode session IDs are persisted locally.  Conversation text
remains in the OpenCode server session and is explicitly deleted when a voice
turn is cancelled, times out, fails, or the daemon restarts.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BUILTIN_TOOL_IDS = (
    "question",
    "bash",
    "read",
    "glob",
    "grep",
    "edit",
    "write",
    "task",
    "webfetch",
    "todowrite",
    "websearch",
    "skill",
    "apply_patch",
)


class OpenCodeError(RuntimeError):
    """An OpenCode server request could not complete safely."""


@dataclass(frozen=True)
class OpenCodePromptResult:
    """A complete assistant reply returned by one synchronous HTTP prompt."""

    text: str
    elapsed_ms: int
    session_id: str


class OpenCodeVoiceAssistant:
    """Manage one short-lived, server-side conversation for the voice daemon."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        provider_id: str,
        model_id: str,
        timeout_sec: float,
        idle_window_sec: float,
        max_turns: int,
        state_path: Path,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_sec = timeout_sec
        self._idle_window_sec = idle_window_sec
        self._max_turns = max_turns
        self._provider_id = provider_id
        self._model_id = model_id
        self._state_path = state_path
        credentials = f"{username}:{password}".encode("utf-8")
        self._authorization = "Basic " + base64.b64encode(credentials).decode("ascii")

        self._request_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._session_id: str | None = None
        self._known_session_ids = self._load_state()
        self._generation = 0
        self._active_requests = 0
        self._completed_turns = 0
        self._last_completed_at: float | None = None

    @property
    def model_label(self) -> str:
        return f"{self._provider_id}/{self._model_id}"

    def _load_state(self) -> set[str]:
        """Read only well-formed opaque IDs; malformed state is ignored safely."""
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[opencode] ignored unreadable session state: {exc}")
            return set()

        session_ids = raw.get("session_ids", []) if isinstance(raw, dict) else []
        if not isinstance(session_ids, list):
            return set()
        return {
            session_id
            for session_id in session_ids
            if isinstance(session_id, str) and session_id.startswith("ses_")
        }

    def _write_state_locked(self) -> None:
        if not self._known_session_ids:
            try:
                self._state_path.unlink(missing_ok=True)
            except OSError as exc:
                print(f"[opencode] could not clear session state: {exc}")
            return

        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._state_path.parent.chmod(0o700)
        except OSError:
            pass
        temporary_path = self._state_path.with_name(
            f".{self._state_path.name}.{uuid.uuid4().hex}.tmp"
        )
        payload = json.dumps(
            {"session_ids": sorted(self._known_session_ids)}, ensure_ascii=False
        )
        descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path.replace(self._state_path)
            self._state_path.chmod(0o600)
        except Exception:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _remember_session_locked(self, session_id: str) -> None:
        self._known_session_ids.add(session_id)
        self._write_state_locked()

    def _forget_session_locked(self, session_id: str) -> None:
        if session_id not in self._known_session_ids:
            return
        self._known_session_ids.remove(session_id)
        self._write_state_locked()

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout_sec: float | None = None,
    ) -> Any:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._base_url + path,
            data=body,
            method=method,
            headers={
                "Authorization": self._authorization,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=timeout_sec if timeout_sec is not None else self._timeout_sec
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise FileNotFoundError(path) from exc
            raise OpenCodeError(f"OpenCode {method} {path} returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise OpenCodeError(f"OpenCode server is unreachable: {exc.reason}") from exc
        except TimeoutError as exc:
            raise OpenCodeError(f"OpenCode {method} {path} timed out") from exc

        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpenCodeError(f"OpenCode {method} {path} returned invalid JSON") from exc

    def _create_session(self) -> str:
        result = self._request_json(
            "POST",
            "/session",
            {
                "title": "AIY Voice",
                "model": {"providerID": self._provider_id, "id": self._model_id},
            },
        )
        session_id = result.get("id") if isinstance(result, dict) else None
        if not isinstance(session_id, str) or not session_id.startswith("ses_"):
            raise OpenCodeError("OpenCode did not return a valid session ID")
        return session_id

    def _end_remote_session(self, session_id: str, reason: str) -> None:
        """Abort then delete one remote session; retain state if cleanup failed."""
        try:
            try:
                self._request_json("POST", f"/session/{urllib.parse.quote(session_id, safe='')}/abort")
            except FileNotFoundError:
                pass
            self._request_json(
                "DELETE", f"/session/{urllib.parse.quote(session_id, safe='')}"
            )
        except FileNotFoundError:
            pass
        except OpenCodeError as exc:
            print(f"[opencode] session cleanup deferred ({reason}): {exc}")
            return

        with self._state_lock:
            self._forget_session_locked(session_id)
        print(f"[opencode] session closed ({reason})")

    def _end_session_in_background(self, session_id: str, reason: str) -> None:
        threading.Thread(
            target=self._end_remote_session,
            args=(session_id, reason),
            name="aiy-opencode-session-close",
            daemon=True,
        ).start()

    def _cleanup_stale_sessions(self) -> None:
        """Delete IDs left by a previous daemon before creating a fresh context."""
        with self._state_lock:
            stale_ids = tuple(
                session_id
                for session_id in self._known_session_ids
                if session_id != self._session_id
            )
        for session_id in stale_ids:
            self._end_remote_session(session_id, "daemon restart")
        with self._state_lock:
            remaining = tuple(
                session_id
                for session_id in self._known_session_ids
                if session_id != self._session_id
            )
        if remaining:
            raise OpenCodeError("could not clean a previous OpenCode voice session")

    def _detach_locked(self) -> str | None:
        session_id = self._session_id
        self._session_id = None
        self._generation += 1
        self._completed_turns = 0
        self._last_completed_at = None
        return session_id

    def _ensure_session(self) -> tuple[str, int]:
        self._cleanup_stale_sessions()
        with self._state_lock:
            if self._session_id is not None:
                return self._session_id, self._generation

        session_id = self._create_session()
        with self._state_lock:
            self._remember_session_locked(session_id)
            self._session_id = session_id
            generation = self._generation
        print(f"[opencode] voice session started ({self.model_label})")
        return session_id, generation

    def prewarm(self) -> None:
        """Create an empty remote session while recording, without blocking GPIO."""
        with self._state_lock:
            if self._session_id is not None:
                return
            expected_generation = self._generation

        def create_in_background() -> None:
            try:
                with self._request_lock:
                    with self._state_lock:
                        if self._generation != expected_generation or self._session_id is not None:
                            return
                    self._ensure_session()
                    with self._state_lock:
                        cancelled = self._generation != expected_generation
                        session_id = self._session_id
                        if cancelled:
                            session_id = self._detach_locked()
                if session_id is not None and cancelled:
                    self._end_session_in_background(session_id, "recording cancelled")
                elif not cancelled:
                    print("[opencode] voice session prewarmed while recording")
            except OpenCodeError as exc:
                print(f"[opencode] prewarm skipped: {exc}")

        threading.Thread(
            target=create_in_background,
            name="aiy-opencode-prewarm",
            daemon=True,
        ).start()

    def ask(self, prompt: str, system_prompt: str) -> OpenCodePromptResult:
        """Send one ASR prompt and wait for its complete text response."""
        if not prompt.strip():
            raise OpenCodeError("OpenCode prompt must not be empty")

        with self._request_lock:
            with self._state_lock:
                previous_session = (
                    self._detach_locked()
                    if self._max_turns > 0 and self._completed_turns >= self._max_turns
                    else None
                )
            if previous_session is not None:
                self._end_remote_session(previous_session, "legacy completed turn limit")

            session_id, request_generation = self._ensure_session()
            with self._state_lock:
                self._active_requests += 1
            started = time.monotonic()
            try:
                result = self._request_json(
                    "POST",
                    f"/session/{urllib.parse.quote(session_id, safe='')}/message",
                    {
                        "model": {
                            "providerID": self._provider_id,
                            "modelID": self._model_id,
                        },
                        "system": system_prompt,
                        "tools": {tool_id: False for tool_id in BUILTIN_TOOL_IDS},
                        "parts": [{"type": "text", "text": prompt}],
                    },
                )
            except Exception:
                with self._state_lock:
                    self._active_requests -= 1
                    current = self._session_id == session_id
                    if current:
                        detached = self._detach_locked()
                    else:
                        detached = None
                if detached is not None:
                    self._end_session_in_background(detached, "request failure")
                raise

            with self._state_lock:
                self._active_requests -= 1
                cancelled = (
                    self._generation != request_generation or self._session_id != session_id
                )
            if cancelled:
                raise OpenCodeError("OpenCode voice turn was cancelled")

        parts = result.get("parts") if isinstance(result, dict) else None
        if not isinstance(parts, list):
            raise OpenCodeError("OpenCode reply did not include message parts")
        text = "".join(
            part.get("text", "")
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
        if not text:
            raise OpenCodeError("OpenCode returned no spoken reply")
        return OpenCodePromptResult(
            text=text,
            elapsed_ms=round((time.monotonic() - started) * 1000),
            session_id=session_id,
        )

    def commit_turn(self) -> None:
        """Retain a reply in context only after its final TTS audio completed."""
        with self._state_lock:
            if self._session_id is None:
                return
            self._completed_turns += 1
            self._last_completed_at = time.monotonic()
            limit = "disabled" if self._max_turns == 0 else str(self._max_turns)
            print(
                f"[memory] OpenCode session saved turn "
                f"({self._completed_turns}/{limit})"
            )

    def expire_if_idle(self, now: float) -> None:
        """Drop the server-side context after the configured idle period."""
        with self._state_lock:
            if (
                self._session_id is None
                or self._active_requests
                or self._last_completed_at is None
                or now - self._last_completed_at < self._idle_window_sec
            ):
                return
            session_id = self._detach_locked()
        if session_id is not None:
            self._end_session_in_background(session_id, "idle timeout")

    def discard_unheard_turn(self) -> None:
        """Abort context if a response was cancelled or failed to play fully."""
        with self._state_lock:
            session_id = self._detach_locked()
        if session_id is not None:
            self._end_session_in_background(session_id, "cancelled or unheard turn")

    def close(self) -> None:
        """Best-effort synchronous cleanup for a systemd daemon restart."""
        with self._state_lock:
            session_id = self._detach_locked()
            stale_ids = tuple(
                stale_id
                for stale_id in self._known_session_ids
                if stale_id != session_id
            )
        for stale_id in stale_ids:
            self._end_remote_session(stale_id, "daemon stop")
        if session_id is not None:
            self._end_remote_session(session_id, "daemon stop")
