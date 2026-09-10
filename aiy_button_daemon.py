#!/usr/bin/env python3
"""Single-owner AIY button daemon for Echo and long-press shutdown."""

import os
import signal
import subprocess
import time

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


def stop_player(player: subprocess.Popen | None) -> None:
    if player and player.poll() is None:
        player.send_signal(signal.SIGTERM)
        try:
            player.wait(timeout=1)
        except subprocess.TimeoutExpired:
            player.kill()


def main() -> int:
    subprocess.run(["pkill", "-f", f"^gpioset -c {GPIO_CHIP}"], check=False)

    card = detect_card(default=1)
    cap_dev = os.getenv("AIY_CAP_DEV", f"hw:{card},0")
    play_dev = os.getenv("AIY_PLAY_DEV", f"plughw:{card},0")

    print("AIY unified button daemon")
    print(f"- Button: {GPIO_CHIP}:{BUTTON_PIN} (active-low)")
    print(f"- LED:    {GPIO_CHIP}:{LED_PIN}")
    print("- Short press: record / stop and Echo")
    print(f"- Shutdown warning: {WARN_SEC:.1f}s")
    print(f"- Shutdown: {SHUTDOWN_SEC:.1f}s")

    led = LedController(GPIO_CHIP, LED_PIN)
    recorder = None
    player = None
    recording = False
    press_started_at = None
    warned = False
    shutdown_requested = False
    led_on = False
    next_led_toggle_at = None

    try:
        led.set(False)
        last_pressed = read_button_pressed(GPIO_CHIP, BUTTON_PIN)

        while True:
            now = time.monotonic()

            if player:
                if player.poll() is not None:
                    player = None
                    led.set(False)
                    print("[echo] playback done")
                elif next_led_toggle_at is not None and now >= next_led_toggle_at:
                    led_on = not led_on
                    led.set(led_on)
                    next_led_toggle_at = now + ECHO_LED_FLASH_SEC

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
                        print("[echo] interrupted by shutdown request")
                        stop_player(player)
                        player = None

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
                    if player:
                        print("[button] short press ignored during Echo playback")
                    elif not recording:
                        WAV_PATH.parent.mkdir(parents=True, exist_ok=True)
                        if WAV_PATH.exists():
                            WAV_PATH.unlink()
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
                            player = start_echo_playback(play_dev, PLAY_WAV_PATH)
                            if player:
                                led_on = True
                                led.set(led_on)
                                next_led_toggle_at = now + ECHO_LED_FLASH_SEC
                            else:
                                led.set(False)
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
        led.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
