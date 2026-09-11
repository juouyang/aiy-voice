#!/usr/bin/env python3
"""Single-owner AIY button daemon for network voice confirmation and shutdown."""

import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from aiy_button_echo import (
    AUDIO_FORMAT,
    BUTTON_PIN,
    CHANNELS,
    GPIO_CHIP,
    LED_PIN,
    PLAY_WAV_PATH,
    SAMPLE_RATE,
    SHORT_PRESS_MAX_SEC,
    WAV_PATH,
    LedController,
    beep_start,
    beep_stop,
    detect_card,
    process_for_playback,
    start_echo_playback,
    stop_recorder,
)
from button_shutdown_guard import (
    SHUTDOWN_SEC,
    WARN_SEC,
    cancel_pattern,
    read_button_pressed,
    shutdown_pattern,
    warn_pattern,
)

POLL_SEC = float(os.getenv("AIY_BUTTON_POLL_SEC", "0.02"))
ECHO_LED_FLASH_SEC = float(os.getenv("AIY_ECHO_LED_FLASH_SEC", "0.25"))
OMLX_BASE_URL = os.getenv("OMLX_BASE_URL", "").rstrip("/")
OMLX_API_KEY = os.getenv("OMLX_API_KEY", "")
OMLX_TIMEOUT_SEC = float(os.getenv("AIY_OMLX_TIMEOUT_SEC", "30"))
OMLX_MAX_UPLOAD_BYTES = int(os.getenv("AIY_OMLX_MAX_UPLOAD_BYTES", "20971520"))
OMLX_ASR_MODEL = os.getenv("AIY_OMLX_ASR_MODEL", "Qwen3-ASR-0.6B-8bit")
OMLX_TTS_MODEL = os.getenv(
    "AIY_OMLX_TTS_MODEL", "Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit"
)
OMLX_TTS_VOICE = os.getenv("AIY_OMLX_TTS_VOICE", "Uncle_Fu")
OMLX_TTS_LANGUAGE = os.getenv("AIY_OMLX_TTS_LANGUAGE", "Chinese")
OMLX_TTS_INSTRUCTIONS = os.getenv("AIY_OMLX_TTS_INSTRUCTIONS", "越清楚越好。")
OMLX_TTS_PREFIX = os.getenv("AIY_OMLX_TTS_PREFIX", "你剛剛說：")


@dataclass(frozen=True)
class VoiceLoopResult:
    job_id: int
    reply_path: Path | None = None
    error: str | None = None


def stop_player(player: subprocess.Popen | None) -> None:
    if player and player.poll() is None:
        player.send_signal(signal.SIGTERM)
        try:
            player.wait(timeout=1)
        except subprocess.TimeoutExpired:
            player.kill()


def delete_file(path: Path | None) -> None:
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def omlx_config_error() -> str | None:
    if not OMLX_BASE_URL:
        return "OMLX_BASE_URL is not configured"
    if not OMLX_API_KEY:
        return "OMLX_API_KEY is not configured"
    return None


def post_omlx(path: str, body: bytes, content_type: str) -> bytes:
    request = urllib.request.Request(
        f"{OMLX_BASE_URL}{path}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {OMLX_API_KEY}",
            "Content-Type": content_type,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=OMLX_TIMEOUT_SEC) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{path} returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{path} is unreachable: {exc.reason}") from exc


def encode_transcription_request(wav_path: Path) -> tuple[bytes, str]:
    if wav_path.stat().st_size > OMLX_MAX_UPLOAD_BYTES:
        raise RuntimeError("recording exceeds the configured upload limit")

    boundary = f"----aiy-voice-{uuid.uuid4().hex}"
    body = bytearray()

    def add_field(name: str, value: str) -> None:
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.extend(value.encode())
        body.extend(b"\r\n")

    add_field("model", OMLX_ASR_MODEL)
    add_field("language", OMLX_TTS_LANGUAGE)
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(
        b'Content-Disposition: form-data; name="file"; filename="recording.wav"\r\n'
    )
    body.extend(b"Content-Type: audio/wav\r\n\r\n")
    body.extend(wav_path.read_bytes())
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())

    return bytes(body), f"multipart/form-data; boundary={boundary}"


def run_voice_loop(
    job_id: int, wav_path: Path, results: queue.Queue[VoiceLoopResult]
) -> None:
    reply_path = PLAY_WAV_PATH.with_name(f"aiy-tts-reply-{job_id}.wav")
    temporary_reply_path = reply_path.with_suffix(".wav.tmp")
    try:
        config_error = omlx_config_error()
        if config_error:
            raise RuntimeError(config_error)

        request_body, content_type = encode_transcription_request(wav_path)
        transcription = json.loads(
            post_omlx("/v1/audio/transcriptions", request_body, content_type)
        )
        transcript = str(transcription.get("text", "")).strip()
        if not transcript:
            raise RuntimeError("ASR returned no text")

        tts_input = f"{OMLX_TTS_PREFIX}{transcript}" if OMLX_TTS_PREFIX else transcript
        tts_request = json.dumps(
            {
                "model": OMLX_TTS_MODEL,
                "input": tts_input,
                "voice": OMLX_TTS_VOICE,
                "language": OMLX_TTS_LANGUAGE,
                "instructions": OMLX_TTS_INSTRUCTIONS,
                "speed": 1.0,
                "response_format": "wav",
            },
            ensure_ascii=False,
        ).encode()
        audio = post_omlx("/v1/audio/speech", tts_request, "application/json")
        if len(audio) <= 44 or audio[:4] != b"RIFF":
            raise RuntimeError("TTS did not return a WAV file")

        temporary_reply_path.write_bytes(audio)
        temporary_reply_path.replace(reply_path)
        results.put(VoiceLoopResult(job_id=job_id, reply_path=reply_path))
    except Exception as exc:
        delete_file(temporary_reply_path)
        delete_file(reply_path)
        results.put(VoiceLoopResult(job_id=job_id, error=str(exc)))
    finally:
        delete_file(wav_path)


def start_voice_loop(
    job_id: int, source_path: Path, results: queue.Queue[VoiceLoopResult]
) -> None:
    worker_path = source_path.with_name(f"aiy-voice-loop-{job_id}.wav")
    shutil.copyfile(source_path, worker_path)
    threading.Thread(
        target=run_voice_loop,
        args=(job_id, worker_path, results),
        name=f"aiy-voice-loop-{job_id}",
        daemon=True,
    ).start()


def main() -> int:
    subprocess.run(["pkill", "-f", f"^gpioset -c {GPIO_CHIP}"], check=False)

    card = detect_card(default=1)
    cap_dev = os.getenv("AIY_CAP_DEV", f"hw:{card},0")
    play_dev = os.getenv("AIY_PLAY_DEV", f"plughw:{card},0")

    print("AIY unified button daemon")
    print(f"- Button: {GPIO_CHIP}:{BUTTON_PIN} (active-low)")
    print(f"- LED:    {GPIO_CHIP}:{LED_PIN}")
    print("- Short press: record, replay, then network voice confirmation")
    print(f"- Shutdown warning: {WARN_SEC:.1f}s")
    print(f"- Shutdown: {SHUTDOWN_SEC:.1f}s")

    led = LedController(GPIO_CHIP, LED_PIN)
    recorder = None
    player = None
    player_kind = None
    tts_playback_path = None
    recording = False
    press_started_at = None
    warned = False
    shutdown_requested = False
    led_on = False
    next_led_toggle_at = None
    results: queue.Queue[VoiceLoopResult] = queue.Queue()
    job_id = 0
    network_pending = False
    tts_reply_path = None
    network_error = None

    def clear_finished_voice_loop() -> None:
        delete_file(WAV_PATH)
        delete_file(PLAY_WAV_PATH)

    def invalidate_voice_loop() -> None:
        nonlocal job_id, network_pending, tts_reply_path, network_error
        job_id += 1
        network_pending = False
        delete_file(tts_reply_path)
        tts_reply_path = None
        network_error = None

    def start_tts_playback() -> None:
        nonlocal player, player_kind, tts_reply_path, tts_playback_path
        if tts_reply_path is None:
            return
        tts_playback_path = tts_reply_path
        tts_reply_path = None
        print("[tts] playback start")
        player = start_echo_playback(play_dev, tts_playback_path)
        if player is not None:
            player_kind = "tts"
        else:
            print("[warn] TTS playback could not start")
            delete_file(tts_playback_path)
            tts_playback_path = None

    try:
        led.set(False)
        last_pressed = read_button_pressed(GPIO_CHIP, BUTTON_PIN)

        while True:
            now = time.monotonic()

            while True:
                try:
                    result = results.get_nowait()
                except queue.Empty:
                    break

                if result.job_id != job_id:
                    delete_file(result.reply_path)
                elif result.error:
                    network_pending = False
                    network_error = result.error
                    print(f"[voice] request failed: {network_error}")
                else:
                    network_pending = False
                    tts_reply_path = result.reply_path
                    print("[voice] TTS response ready")

            if player and player.poll() is not None:
                finished_kind = player_kind
                player = None
                player_kind = None
                if finished_kind == "recording":
                    print("[echo] original playback done")
                elif finished_kind == "tts":
                    print("[tts] playback done")
                    delete_file(tts_playback_path)
                    tts_playback_path = None
                    clear_finished_voice_loop()

            if player is None and tts_reply_path is not None and not recording:
                start_tts_playback()

            if player is None and network_error and not network_pending and not recording:
                print("[voice] returning to standby after failed request")
                cancel_pattern()
                network_error = None
                clear_finished_voice_loop()

            blinking = bool(player or network_pending or tts_reply_path)
            if blinking:
                if next_led_toggle_at is None:
                    led_on = True
                    led.set(led_on)
                    next_led_toggle_at = now + ECHO_LED_FLASH_SEC
                elif now >= next_led_toggle_at:
                    led_on = not led_on
                    led.set(led_on)
                    next_led_toggle_at = now + ECHO_LED_FLASH_SEC
            else:
                next_led_toggle_at = None
                if not recording and not warned:
                    led.set(False)

            pressed = read_button_pressed(GPIO_CHIP, BUTTON_PIN)

            if pressed and not last_pressed:
                press_started_at = now
                warned = False
                shutdown_requested = False

            elif pressed and press_started_at is not None:
                held = now - press_started_at
                if held >= WARN_SEC and not warned:
                    if recording:
                        print("[rec] interrupted by shutdown request")
                        stop_recorder(recorder)
                        recorder = None
                        recording = False
                    if player:
                        print("[voice] playback interrupted by shutdown request")
                        stop_player(player)
                        player = None
                        player_kind = None
                    delete_file(tts_playback_path)
                    tts_playback_path = None
                    invalidate_voice_loop()
                    clear_finished_voice_loop()

                    warned = True
                    print("[warn] long-press detected, shutdown soon")
                    warn_pattern()
                    led.set(True)

                if held >= SHUTDOWN_SEC and warned and not shutdown_requested:
                    shutdown_requested = True
                    print("[shutdown] executing safe shutdown")
                    shutdown_pattern()
                    subprocess.run(["sudo", "/sbin/shutdown", "-h", "now"], check=False)
                    return 0

            elif not pressed and last_pressed:
                held = (now - press_started_at) if press_started_at is not None else 0.0

                if held <= SHORT_PRESS_MAX_SEC and not warned:
                    if player or network_pending or tts_reply_path or network_error:
                        print("[button] short press ignored while voice confirmation is active")
                    elif not recording:
                        delete_file(WAV_PATH)
                        delete_file(PLAY_WAV_PATH)
                        print("[rec] start")
                        beep_start()
                        led.set(True)
                        recorder = subprocess.Popen(
                            [
                                "arecord",
                                "-D",
                                cap_dev,
                                "-f",
                                AUDIO_FORMAT,
                                "-r",
                                str(SAMPLE_RATE),
                                "-c",
                                str(CHANNELS),
                                "-q",
                                str(WAV_PATH),
                            ]
                        )
                        recording = True
                    else:
                        print("[rec] stop")
                        stop_recorder(recorder)
                        recorder = None
                        recording = False
                        beep_stop()

                        if WAV_PATH.exists() and WAV_PATH.stat().st_size > 44:
                            channel, peak_pct, auto = process_for_playback(
                                WAV_PATH, PLAY_WAV_PATH
                            )
                            print(
                                f"[proc] channel={channel} input_peak={peak_pct * 100:.2f}% auto_gain={auto:.2f}x"
                            )
                            job_id += 1
                            network_pending = True
                            network_error = None
                            # Start local playback before copying the WAV for
                            # the network worker, so Echo stays immediate.
                            player = start_echo_playback(play_dev, PLAY_WAV_PATH)
                            if player is not None:
                                player_kind = "recording"
                            else:
                                print("[warn] original playback could not start")
                            try:
                                start_voice_loop(job_id, PLAY_WAV_PATH, results)
                                print("[voice] ASR and TTS request started")
                            except OSError as exc:
                                network_pending = False
                                network_error = f"could not start voice request: {exc}"
                                print(f"[voice] request failed: {network_error}")
                        else:
                            print("[warn] no valid audio recorded")
                            led.set(False)

                elif warned and held < SHUTDOWN_SEC:
                    print("[cancel] shutdown cancelled")
                    cancel_pattern()
                    led.set(False)

                elif not recording and not player:
                    led.set(False)

                press_started_at = None
                warned = False
                shutdown_requested = False

            last_pressed = pressed
            time.sleep(POLL_SEC)

    except KeyboardInterrupt:
        print("\nExiting...")
    finally:
        stop_recorder(recorder)
        stop_player(player)
        delete_file(tts_reply_path)
        delete_file(tts_playback_path)
        led.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
