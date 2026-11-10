#!/usr/bin/env python3
"""Minimal OpenAI Realtime WebSocket client for the AIY push-to-talk flow.

The AIY daemon owns recording, button handling and ALSA playback.  This module
only converts the already-recorded WAV to the Realtime API's required 24 kHz
mono PCM, keeps an in-memory conversation WebSocket, and returns PCM audio and
transcripts.  It deliberately contains no local model, shell tool or web tool.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

try:
    import websocket
except ImportError:  # pragma: no cover - exercised by deployment preflight.
    websocket = None


class OpenAIRealtimeError(RuntimeError):
    """The OpenAI Realtime connection or response failed."""


@dataclass(frozen=True)
class OpenAIRealtimeResult:
    """Completed Realtime audio response and its best-effort transcripts."""

    input_transcript: str
    output_transcript: str
    output_pcm: bytes
    elapsed_ms: int


def wav_to_pcm24k_mono(path: Path) -> bytes:
    """Convert the verified AIY 48 kHz S32_LE WAV capture to 24 kHz mono PCM16."""
    try:
        with wave.open(str(path), "rb") as reader:
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            sample_rate = reader.getframerate()
            compression = reader.getcomptype()
            frames = reader.readframes(reader.getnframes())
    except (OSError, wave.Error) as exc:
        raise OpenAIRealtimeError(f"could not read recorded WAV: {exc}") from exc

    if compression != "NONE":
        raise OpenAIRealtimeError("recorded WAV must be uncompressed PCM")
    if sample_rate != 48000 or sample_width != 4 or channels not in (1, 2):
        raise OpenAIRealtimeError(
            "Realtime input currently requires the AIY 48 kHz S32_LE mono/stereo WAV"
        )

    frame_width = sample_width * channels
    if len(frames) % frame_width:
        raise OpenAIRealtimeError("recorded WAV has an incomplete audio frame")

    output = bytearray()
    # The source rate is precisely double 24 kHz.  Average each adjacent pair
    # after downmixing; this is deterministic, lightweight, and sufficient for
    # spoken-word capture from the AIY Voice HAT.
    mono_samples: list[int] = []
    for offset in range(0, len(frames), frame_width):
        values = [
            int.from_bytes(
                frames[offset + channel * sample_width : offset + (channel + 1) * sample_width],
                "little",
                signed=True,
            )
            for channel in range(channels)
        ]
        mono_samples.append(sum(values) // channels)

    for index in range(0, len(mono_samples) - 1, 2):
        sample_32 = (mono_samples[index] + mono_samples[index + 1]) // 2
        sample_16 = max(-32768, min(32767, sample_32 >> 16))
        output.extend(sample_16.to_bytes(2, "little", signed=True))
    if not output:
        raise OpenAIRealtimeError("recorded WAV contains no usable audio")
    return bytes(output)


def write_pcm24k_wav(path: Path, pcm: bytes) -> None:
    """Write API PCM16 output as a normal WAV for the established ALSA path."""
    if not pcm or len(pcm) % 2:
        raise OpenAIRealtimeError("Realtime returned invalid PCM audio")
    try:
        with wave.open(str(path), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(24000)
            writer.writeframes(pcm)
    except (OSError, wave.Error) as exc:
        raise OpenAIRealtimeError(f"could not write Realtime WAV: {exc}") from exc


class OpenAIRealtimeVoiceAssistant:
    """One in-memory Realtime conversation, reset after idle/cancellation."""

    def __init__(
        self,
        api_key: str,
        model: str,
        voice: str,
        timeout_sec: float,
        idle_window_sec: float,
        transcription_model: str,
        safety_identifier: str = "",
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._voice = voice
        self._timeout_sec = timeout_sec
        self._idle_window_sec = idle_window_sec
        self._transcription_model = transcription_model
        # Realtime recommends a stable, privacy-preserving identifier.  Never
        # transmit the hostname itself; use an explicit override or its hash.
        self._safety_identifier = safety_identifier or hashlib.sha256(
            socket.gethostname().encode("utf-8")
        ).hexdigest()
        self._lock = threading.RLock()
        self._socket_lock = threading.Lock()
        self._socket = None
        self._last_completed_at: float | None = None
        self._closed = False

    @property
    def model_label(self) -> str:
        return self._model

    def _connect_locked(self) -> None:
        if self._closed:
            raise OpenAIRealtimeError("Realtime voice assistant is closed")
        if websocket is None:
            raise OpenAIRealtimeError(
                "websocket-client is not installed; run pip install -r requirements.txt"
            )
        if self._socket is not None:
            return
        url = f"wss://api.openai.com/v1/realtime?model={self._model}"
        try:
            connection = websocket.create_connection(
                url,
                timeout=self._timeout_sec,
                header=[
                    f"Authorization: Bearer {self._api_key}",
                    f"OpenAI-Safety-Identifier: {self._safety_identifier}",
                ],
            )
        except Exception as exc:
            raise OpenAIRealtimeError(f"could not connect to OpenAI Realtime: {exc}") from exc
        with self._socket_lock:
            if self._closed:
                connection.close()
                raise OpenAIRealtimeError("Realtime voice assistant is closed")
            self._socket = connection
        print(f"[realtime] connected ({self._model})")

    def _send_locked(self, event: dict) -> None:
        if self._socket is None:
            raise OpenAIRealtimeError("Realtime connection is not available")
        try:
            self._socket.send(json.dumps(event, ensure_ascii=False))
        except Exception as exc:
            self._close_locked()
            raise OpenAIRealtimeError(f"could not send Realtime event: {exc}") from exc

    def _recv_locked(self) -> dict:
        if self._socket is None:
            raise OpenAIRealtimeError("Realtime connection is not available")
        try:
            raw = self._socket.recv()
        except Exception as exc:
            self._close_locked()
            raise OpenAIRealtimeError(f"Realtime connection ended: {exc}") from exc
        try:
            message = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise OpenAIRealtimeError("Realtime returned invalid JSON") from exc
        if not isinstance(message, dict):
            raise OpenAIRealtimeError("Realtime returned an invalid event")
        return message

    def _close_locked(self) -> None:
        with self._socket_lock:
            connection, self._socket = self._socket, None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    def prewarm(self) -> None:
        """Open the session during recording so Echo masks connection latency."""
        def connect_in_background() -> None:
            try:
                with self._lock:
                    self._connect_locked()
            except OpenAIRealtimeError as exc:
                print(f"[realtime] prewarm skipped: {exc}")

        threading.Thread(
            target=connect_in_background,
            name="aiy-realtime-prewarm",
            daemon=True,
        ).start()

    def ask_wav(self, wav_path: Path, system_prompt: str) -> OpenAIRealtimeResult:
        """Commit one push-to-talk recording and wait for its streamed reply."""
        input_pcm = wav_to_pcm24k_mono(wav_path)
        started = time.monotonic()
        with self._lock:
            self._connect_locked()
            # This application already decides the start and end of a turn with
            # its two-short-press UI, so disable VAD and commit explicitly.
            self._send_locked(
                {
                    "type": "session.update",
                    "session": {
                        "type": "realtime",
                        "instructions": system_prompt,
                        "audio": {
                            "input": {
                                "format": {"type": "audio/pcm", "rate": 24000},
                                "transcription": {
                                    "model": self._transcription_model,
                                    "language": "zh",
                                },
                                "turn_detection": None,
                            },
                            "output": {
                                "format": {"type": "audio/pcm", "rate": 24000},
                                "voice": self._voice,
                            },
                        },
                    },
                }
            )
            # Do not reuse incomplete input left by a lost/cancelled turn.
            self._send_locked({"type": "input_audio_buffer.clear"})
            chunk_size = 24_000  # 0.5 seconds of PCM16; far below the API limit.
            for offset in range(0, len(input_pcm), chunk_size):
                self._send_locked(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(input_pcm[offset : offset + chunk_size]).decode(
                            "ascii"
                        ),
                    }
                )
            self._send_locked({"type": "input_audio_buffer.commit"})
            self._send_locked({"type": "response.create"})

            input_transcript = ""
            output_transcript_parts: list[str] = []
            output_pcm = bytearray()
            response_started = False
            deadline = time.monotonic() + self._timeout_sec
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._close_locked()
                    raise OpenAIRealtimeError("Realtime response timed out")
                try:
                    self._socket.settimeout(remaining)
                except Exception:
                    pass
                event = self._recv_locked()
                event_type = event.get("type")
                if event_type == "error":
                    error = event.get("error", {})
                    message = error.get("message") if isinstance(error, dict) else None
                    raise OpenAIRealtimeError(f"Realtime API error: {message or event}")
                if event_type == "response.created":
                    response_started = True
                elif event_type == "conversation.item.input_audio_transcription.completed":
                    input_transcript = str(event.get("transcript", "")).strip()
                elif event_type == "response.output_audio.delta":
                    try:
                        output_pcm.extend(base64.b64decode(event.get("delta", ""), validate=True))
                    except Exception as exc:
                        raise OpenAIRealtimeError("Realtime returned invalid audio data") from exc
                elif event_type == "response.output_audio_transcript.delta":
                    output_transcript_parts.append(str(event.get("delta", "")))
                elif event_type == "response.done" and response_started:
                    response = event.get("response", {})
                    if not isinstance(response, dict) or response.get("status") != "completed":
                        status = response.get("status") if isinstance(response, dict) else "unknown"
                        raise OpenAIRealtimeError(f"Realtime response ended with status {status}")
                    break

            if not output_pcm:
                raise OpenAIRealtimeError("Realtime returned no output audio")
            self._last_completed_at = time.monotonic()
            return OpenAIRealtimeResult(
                input_transcript=input_transcript,
                output_transcript="".join(output_transcript_parts).strip(),
                output_pcm=bytes(output_pcm),
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )

    def commit_turn(self) -> None:
        """The Realtime session already retains completed conversation state."""

    def expire_if_idle(self, now: float) -> None:
        with self._lock:
            if (
                self._last_completed_at is not None
                and now - self._last_completed_at >= self._idle_window_sec
            ):
                print("[realtime] conversation expired after idle timeout")
                self._last_completed_at = None
                self._close_locked()

    def discard_unheard_turn(self) -> None:
        """Close the session: an unheard reply must not become later context."""
        with self._socket_lock:
            connection, self._socket = self._socket, None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        self._last_completed_at = None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._close_locked()
