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
import urllib.parse
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
    scale_wav_for_playback,
    start_echo_playback,
    stop_recorder,
)
from button_shutdown_guard import (
    SHUTDOWN_SEC,
    WARN_SEC,
    cancel_pattern,
    play_tone,
    read_button_pressed,
    shutdown_pattern,
    warn_pattern,
)

PROJECT_DIR = Path(__file__).resolve().parent
POLL_SEC = float(os.getenv("AIY_BUTTON_POLL_SEC", "0.02"))
ECHO_LED_FLASH_SEC = float(os.getenv("AIY_ECHO_LED_FLASH_SEC", "0.25"))
MAX_RECORDING_SEC = float(os.getenv("AIY_MAX_RECORDING_SEC", "45"))
SECONDARY_HOLD_MIN_SEC = float(os.getenv("AIY_SECONDARY_HOLD_MIN_SEC", "1.5"))
SECONDARY_CONFIRM_SEC = float(os.getenv("AIY_SECONDARY_CONFIRM_SEC", "1.0"))
SECONDARY_FLASH_SEC = float(os.getenv("AIY_SECONDARY_FLASH_SEC", "0.10"))
SECONDARY_RELEASE_PROMPT_OVERRIDE = os.getenv("AIY_SECONDARY_RELEASE_PROMPT_WAV")
OUTPUT_VOLUME_CONFIG_PATH = Path(
    os.getenv(
        "AIY_OUTPUT_VOLUME_CONFIG",
        str(Path.home() / ".config" / "aiy-voice" / "output-volume.env"),
    )
)
OUTPUT_VOLUME_PROFILES = ("quiet", "normal", "loud")
OUTPUT_VOLUME_GAINS = {"quiet": 0.35, "normal": 0.65, "loud": 1.00}
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
NTFY_BASE_URL = os.getenv("NTFY_BASE_URL", "").rstrip("/")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
NTFY_TIMEOUT_SEC = float(os.getenv("AIY_NTFY_TIMEOUT_SEC", "5"))


@dataclass(frozen=True)
class VoiceLoopResult:
    job_id: int
    reply_path: Path | None = None
    error: str | None = None


def secondary_prompt_pattern(output_gain: float = 1.0) -> None:
    """Signal that one short press may confirm the auxiliary gesture."""
    play_tone(660, 0.07, output_gain)
    play_tone(880, 0.07, output_gain)


def secondary_confirm_pattern(output_gain: float = 1.0) -> None:
    """Acknowledge an auxiliary gesture until it receives a real action."""
    play_tone(880, 0.07, output_gain)
    play_tone(1040, 0.07, output_gain)


def fixed_prompt_path(profile: str, filename: str) -> Path:
    return PROJECT_DIR / "assets" / "gain" / profile / filename


def is_valid_wav(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 44
    except OSError:
        return False


def load_output_volume_profile() -> str:
    """Load the device-local fixed-prompt volume profile."""
    try:
        for line in OUTPUT_VOLUME_CONFIG_PATH.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if key == "AIY_OUTPUT_VOLUME_PROFILE" and separator:
                profile = value.strip()
                if profile in OUTPUT_VOLUME_PROFILES:
                    return profile
                print(
                    f"[warn] ignoring invalid output volume profile in "
                    f"{OUTPUT_VOLUME_CONFIG_PATH}"
                )
                break
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"[warn] could not read output volume setting: {exc}")

    # "loud" is the previous, unscaled TTS cue volume, preserving the
    # verified behavior on the first deployment of this feature.
    return "loud"


def save_output_volume_profile(profile: str) -> None:
    """Atomically save the selected profile without keeping it in Git."""
    if profile not in OUTPUT_VOLUME_PROFILES:
        raise ValueError(f"unknown output volume profile: {profile}")

    OUTPUT_VOLUME_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = OUTPUT_VOLUME_CONFIG_PATH.with_name(
        f".{OUTPUT_VOLUME_CONFIG_PATH.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary_path.write_text(
            f"AIY_OUTPUT_VOLUME_PROFILE={profile}\n", encoding="utf-8"
        )
        temporary_path.chmod(0o600)
        temporary_path.replace(OUTPUT_VOLUME_CONFIG_PATH)
    finally:
        delete_file(temporary_path)


def next_output_volume_profile(profile: str) -> str:
    try:
        profile_index = OUTPUT_VOLUME_PROFILES.index(profile)
    except ValueError:
        return "loud"
    return OUTPUT_VOLUME_PROFILES[(profile_index + 1) % len(OUTPUT_VOLUME_PROFILES)]


def start_secondary_release_prompt(
    play_dev: str, profile: str
) -> subprocess.Popen | None:
    """Play the cached TTS release cue, with a local-tone fallback."""
    prompt_path = (
        Path(SECONDARY_RELEASE_PROMPT_OVERRIDE)
        if SECONDARY_RELEASE_PROMPT_OVERRIDE
        else fixed_prompt_path(profile, "secondary-release-prompt.wav")
    )

    if is_valid_wav(prompt_path):
        print(
            "[button] auxiliary gesture ready; "
            f"playing {profile} release prompt"
        )
        return start_echo_playback(play_dev, prompt_path)

    print("[warn] release prompt is unavailable; using tone fallback")
    secondary_prompt_pattern(OUTPUT_VOLUME_GAINS[profile])
    return None


def start_volume_announcement(
    play_dev: str, profile: str
) -> subprocess.Popen | None:
    """Play the matching pre-rendered announcement for the selected profile."""
    prompt_path = fixed_prompt_path(profile, f"volume-{profile}.wav")
    if is_valid_wav(prompt_path):
        print(
            f"[volume] selected {profile} "
            f"({OUTPUT_VOLUME_GAINS[profile]:.2f}x); playing announcement"
        )
        return start_echo_playback(play_dev, prompt_path)

    print("[warn] volume announcement is unavailable; using tone fallback")
    secondary_confirm_pattern(OUTPUT_VOLUME_GAINS[profile])
    return None


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


def publish_ntfy_transcript(transcript: str) -> None:
    """Best-effort ASR transcript notification that never affects voice flow."""
    if not NTFY_BASE_URL or not NTFY_TOPIC:
        print("[ntfy] notification skipped: NTFY_BASE_URL or NTFY_TOPIC is missing")
        return

    try:
        request = urllib.request.Request(
            f"{NTFY_BASE_URL}/{urllib.parse.quote(NTFY_TOPIC, safe='')}",
            data=transcript.encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Title": "AIY Voice",
                "Tags": "microphone",
                "Priority": "default",
            },
        )
        with urllib.request.urlopen(request, timeout=NTFY_TIMEOUT_SEC) as response:
            response.read()
        print("[ntfy] ASR transcript notification sent")
    except Exception as exc:
        print(f"[ntfy] notification failed (ignored): {exc}")


def start_ntfy_transcript_notification(job_id: int, transcript: str) -> None:
    """Dispatch ntfy work separately so TTS never waits for it."""
    try:
        threading.Thread(
            target=publish_ntfy_transcript,
            args=(transcript,),
            name=f"aiy-ntfy-{job_id}",
            daemon=True,
        ).start()
    except RuntimeError as exc:
        print(f"[ntfy] notification could not start (ignored): {exc}")


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

        start_ntfy_transcript_notification(job_id, transcript)

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
    print("- Short press: record/stop; cancel an active voice confirmation")
    print(f"- Recording auto-stop: {MAX_RECORDING_SEC:.1f}s")
    print(
        f"- Auxiliary gesture: release after the {SECONDARY_HOLD_MIN_SEC:.1f}s prompt, then short-press within {SECONDARY_CONFIRM_SEC:.1f}s"
    )
    print(
        "- Auxiliary volume: "
        "quiet 0.35x, normal 0.65x, loud 1.00x (fixed prompt WAVs only)"
    )
    print(f"- Shutdown warning: {WARN_SEC:.1f}s")
    print(f"- Shutdown: {SHUTDOWN_SEC:.1f}s")

    led = LedController(GPIO_CHIP, LED_PIN)
    recorder = None
    player = None
    player_kind = None
    tts_playback_path = None
    release_prompt_player = None
    recording = False
    recording_started_at = None
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
    secondary_pending = False
    secondary_deadline = None
    secondary_flash_edges_remaining = 0
    secondary_next_flash_at = None
    secondary_wait_led_set = False
    secondary_hold_ready = False
    secondary_release_pending = False
    secondary_armed_at = None
    secondary_released_at = None
    secondary_confirmation_queued = False
    output_volume_profile = load_output_volume_profile()
    print(
        f"- Current auxiliary volume: {output_volume_profile} "
        f"({OUTPUT_VOLUME_GAINS[output_volume_profile]:.2f}x)"
    )

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

    def voice_turn_is_active() -> bool:
        """Return whether a completed recording still has audible work pending."""
        return (
            player_kind in ("recording", "tts")
            or network_pending
            or tts_reply_path is not None
            or network_error is not None
        )

    def cancel_voice_turn() -> None:
        """Immediately return to standby without allowing a stale reply to play."""
        nonlocal player, player_kind, tts_playback_path

        print("[voice] current turn cancelled by button")
        if player_kind in ("recording", "tts"):
            stop_player(player)
            player = None
            player_kind = None
        delete_file(tts_playback_path)
        tts_playback_path = None
        invalidate_voice_loop()
        clear_finished_voice_loop()
        led.set(False)
        # The player has already stopped; this is only an audible cancellation
        # acknowledgement and never delays stopping the caller's audio.
        cancel_pattern(OUTPUT_VOLUME_GAINS[output_volume_profile])

    def start_secondary_wait() -> None:
        nonlocal secondary_pending, secondary_deadline
        nonlocal secondary_flash_edges_remaining, secondary_next_flash_at
        nonlocal secondary_wait_led_set, secondary_armed_at, led_on
        secondary_pending = True
        secondary_wait_led_set = False
        led_on = False
        led.set(False)
        print("[button] auxiliary gesture armed; short-press to confirm")
        secondary_prompt_pattern(OUTPUT_VOLUME_GAINS[output_volume_profile])
        armed_at = time.monotonic()
        secondary_armed_at = armed_at
        secondary_deadline = armed_at + SECONDARY_CONFIRM_SEC
        secondary_flash_edges_remaining = 4
        secondary_next_flash_at = armed_at

    def clear_secondary_wait() -> None:
        nonlocal secondary_pending, secondary_deadline
        nonlocal secondary_flash_edges_remaining, secondary_next_flash_at
        nonlocal secondary_wait_led_set, secondary_armed_at
        nonlocal secondary_released_at, secondary_confirmation_queued
        secondary_pending = False
        secondary_deadline = None
        secondary_armed_at = None
        secondary_released_at = None
        secondary_confirmation_queued = False
        secondary_flash_edges_remaining = 0
        secondary_next_flash_at = None
        secondary_wait_led_set = False

    def start_tts_playback() -> None:
        nonlocal player, player_kind, tts_reply_path, tts_playback_path
        if tts_reply_path is None:
            return
        source_path = tts_reply_path
        tts_reply_path = None
        output_gain = OUTPUT_VOLUME_GAINS[output_volume_profile]
        if output_gain == 1.0:
            tts_playback_path = source_path
        else:
            scaled_path = source_path.with_name(f"{source_path.stem}-output.wav")
            try:
                scale_wav_for_playback(source_path, scaled_path, output_gain)
            except Exception as exc:
                print(f"[warn] could not scale TTS output; playing original: {exc}")
                delete_file(scaled_path)
                tts_playback_path = source_path
            else:
                delete_file(source_path)
                tts_playback_path = scaled_path
        print("[tts] playback start")
        player = start_echo_playback(play_dev, tts_playback_path)
        if player is not None:
            player_kind = "tts"
        else:
            print("[warn] TTS playback could not start")
            delete_file(tts_playback_path)
            tts_playback_path = None

    def finish_recording(reason: str) -> None:
        """Stop recording and start the normal Echo/ASR/TTS sequence."""
        nonlocal recorder, recording, recording_started_at
        nonlocal job_id, network_pending, network_error, player, player_kind
        if not recording:
            return

        print(f"[rec] stop ({reason})")
        stop_recorder(recorder)
        recorder = None
        recording = False
        recording_started_at = None
        beep_stop(OUTPUT_VOLUME_GAINS[output_volume_profile])

        if WAV_PATH.exists() and WAV_PATH.stat().st_size > 44:
            channel, peak_pct, auto = process_for_playback(
                WAV_PATH,
                PLAY_WAV_PATH,
                OUTPUT_VOLUME_GAINS[output_volume_profile],
            )
            print(
                f"[proc] channel={channel} input_peak={peak_pct * 100:.2f}% auto_gain={auto:.2f}x"
            )
            job_id += 1
            network_pending = True
            network_error = None
            # Start local playback before copying the WAV for the network
            # worker, so Echo stays immediate.
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

    def trigger_auxiliary_volume() -> None:
        nonlocal output_volume_profile, player, player_kind
        clear_secondary_wait()
        led.set(False)
        output_volume_profile = next_output_volume_profile(output_volume_profile)
        try:
            save_output_volume_profile(output_volume_profile)
        except OSError as exc:
            print(f"[warn] could not save output volume setting: {exc}")
        print("[button] auxiliary volume gesture triggered")
        player = start_volume_announcement(play_dev, output_volume_profile)
        if player is not None:
            player_kind = "volume"

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

            if (
                recording
                and recording_started_at is not None
                and now - recording_started_at >= MAX_RECORDING_SEC
            ):
                print(f"[rec] maximum duration reached ({MAX_RECORDING_SEC:.1f}s)")
                finish_recording("maximum duration")

            if (
                release_prompt_player is not None
                and release_prompt_player.poll() is not None
            ):
                release_prompt_player = None
                if secondary_release_pending:
                    secondary_release_pending = False
                    start_secondary_wait()
                    if secondary_confirmation_queued:
                        print("[button] using queued auxiliary confirmation")
                        trigger_auxiliary_volume()

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
                elif finished_kind == "volume":
                    print("[volume] announcement done")

            if player is None and tts_reply_path is not None and not recording:
                start_tts_playback()

            if player is None and network_error and not network_pending and not recording:
                print("[voice] returning to standby after failed request")
                cancel_pattern(OUTPUT_VOLUME_GAINS[output_volume_profile])
                network_error = None
                clear_finished_voice_loop()

            if (
                secondary_pending
                and secondary_deadline is not None
                and now >= secondary_deadline
                and not last_pressed
            ):
                print("[button] auxiliary gesture expired")
                clear_secondary_wait()

            if secondary_pending:
                if (
                    secondary_flash_edges_remaining > 0
                    and secondary_next_flash_at is not None
                    and now >= secondary_next_flash_at
                ):
                    led_on = not led_on
                    led.set(led_on)
                    secondary_flash_edges_remaining -= 1
                    secondary_next_flash_at = now + SECONDARY_FLASH_SEC
                elif (
                    secondary_flash_edges_remaining == 0
                    and not secondary_wait_led_set
                ):
                    led_on = True
                    led.set(led_on)
                    secondary_wait_led_set = True
            elif secondary_hold_ready or secondary_release_pending:
                if not led_on:
                    led_on = True
                    led.set(led_on)
            else:
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
                secondary_hold_ready = False

            elif pressed and press_started_at is not None:
                held = now - press_started_at
                if (
                    held >= SECONDARY_HOLD_MIN_SEC
                    and not secondary_hold_ready
                    and not secondary_pending
                    and not secondary_release_pending
                    and not warned
                    and not recording
                    and player is None
                    and not network_pending
                    and tts_reply_path is None
                    and not network_error
                ):
                    secondary_hold_ready = True
                    led_on = True
                    led.set(led_on)
                    release_prompt_player = start_secondary_release_prompt(
                        play_dev, output_volume_profile
                    )

                if held >= WARN_SEC and not warned:
                    if recording:
                        print("[rec] interrupted by shutdown request")
                        stop_recorder(recorder)
                        recorder = None
                        recording = False
                        recording_started_at = None
                    if player:
                        print("[voice] playback interrupted by shutdown request")
                        stop_player(player)
                        player = None
                        player_kind = None
                    delete_file(tts_playback_path)
                    tts_playback_path = None
                    invalidate_voice_loop()
                    clear_secondary_wait()
                    secondary_hold_ready = False
                    secondary_release_pending = False
                    secondary_released_at = None
                    secondary_confirmation_queued = False
                    stop_player(release_prompt_player)
                    release_prompt_player = None
                    clear_finished_voice_loop()

                    warned = True
                    print("[warn] long-press detected, shutdown soon")
                    warn_pattern(OUTPUT_VOLUME_GAINS[output_volume_profile])
                    led.set(True)

                if held >= SHUTDOWN_SEC and warned and not shutdown_requested:
                    shutdown_requested = True
                    print("[shutdown] executing safe shutdown")
                    shutdown_pattern(OUTPUT_VOLUME_GAINS[output_volume_profile])
                    subprocess.run(["sudo", "/sbin/shutdown", "-h", "now"], check=False)
                    return 0

            elif not pressed and last_pressed:
                held = (now - press_started_at) if press_started_at is not None else 0.0

                if secondary_pending:
                    if (
                        held <= SHORT_PRESS_MAX_SEC
                        and press_started_at is not None
                        and secondary_released_at is not None
                        and secondary_deadline is not None
                        and press_started_at >= secondary_released_at
                        and press_started_at <= secondary_deadline
                    ):
                        trigger_auxiliary_volume()
                    else:
                        print("[button] auxiliary gesture confirmation ignored")

                elif not warned and secondary_hold_ready:
                    secondary_hold_ready = False
                    secondary_released_at = now
                    secondary_confirmation_queued = False
                    if (
                        release_prompt_player is not None
                        and release_prompt_player.poll() is None
                    ):
                        secondary_release_pending = True
                        print(
                            "[button] auxiliary release received; "
                            "waiting for release prompt to finish"
                        )
                    else:
                        release_prompt_player = None
                        start_secondary_wait()

                elif secondary_release_pending:
                    if (
                        held <= SHORT_PRESS_MAX_SEC
                        and press_started_at is not None
                        and secondary_released_at is not None
                        and press_started_at >= secondary_released_at
                    ):
                        secondary_confirmation_queued = True
                        print("[button] auxiliary confirmation queued")
                    else:
                        print("[button] auxiliary gesture confirmation ignored")

                elif held <= SHORT_PRESS_MAX_SEC and not warned:
                    if not recording and voice_turn_is_active():
                        cancel_voice_turn()
                    elif player is not None:
                        print("[button] short press ignored while volume announcement is active")
                    elif not recording:
                        delete_file(WAV_PATH)
                        delete_file(PLAY_WAV_PATH)
                        print("[rec] start")
                        beep_start(OUTPUT_VOLUME_GAINS[output_volume_profile])
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
                        recording_started_at = time.monotonic()
                    else:
                        finish_recording("button")

                elif warned and held < SHUTDOWN_SEC:
                    print("[cancel] shutdown cancelled")
                    cancel_pattern(OUTPUT_VOLUME_GAINS[output_volume_profile])
                    led.set(False)

                elif not recording and not player:
                    led.set(False)

                press_started_at = None
                warned = False
                shutdown_requested = False
                secondary_hold_ready = False

            last_pressed = pressed
            time.sleep(POLL_SEC)

    except KeyboardInterrupt:
        print("\nExiting...")
    finally:
        stop_recorder(recorder)
        stop_player(player)
        stop_player(release_prompt_player)
        delete_file(tts_reply_path)
        delete_file(tts_playback_path)
        led.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
