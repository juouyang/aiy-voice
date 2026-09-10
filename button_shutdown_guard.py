#!/usr/bin/env python3
import math
import os
import subprocess
import struct
import time
import wave

BUTTON_PIN = int(os.getenv("AIY_BUTTON_PIN", "23"))
LED_PIN = int(os.getenv("AIY_LED_PIN", "25"))
GPIO_CHIP = os.getenv("AIY_GPIO_CHIP", "gpiochip0")
WARN_SEC = float(os.getenv("AIY_SHUTDOWN_WARN_SEC", "10.0"))
SHUTDOWN_SEC = float(os.getenv("AIY_SHUTDOWN_SEC", "12.0"))
BEEP_DEVICE = os.getenv("AIY_BEEP_DEV", "hw:1,0")
BEEP_PATH = os.getenv("AIY_BEEP_WAV", "/tmp/aiy_shutdown_guard_beep.wav")
BEEP_PERCENT = float(os.getenv("AIY_BEEP_PERCENT", "60"))
SHORT_PRESS_MAX_SEC = float(os.getenv("AIY_SHORT_PRESS_MAX_SEC", "1.0"))
POLL_SEC = float(os.getenv("AIY_BUTTON_POLL_SEC", "0.05"))


class LedController:
    def __init__(self, chip: str, pin: int):
        self.chip = chip
        self.pin = pin
        self.proc = None

    def set(self, on: bool) -> None:
        self._stop_holder()
        value = "1" if on else "0"
        self.proc = subprocess.Popen(
            ["gpioset", "-c", self.chip, f"{self.pin}={value}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.02)

    def _stop_holder(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def close(self) -> None:
        self.set(False)


def run_output(cmd: list[str]) -> str:
    return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()


def read_button_pressed(chip: str, pin: int) -> bool:
    try:
        return run_output(["gpioget", "-c", chip, "--numeric", str(pin)]) == "0"
    except subprocess.CalledProcessError:
        time.sleep(0.05)
        return False


def play_tone(freq: int, dur_sec: float = 0.12) -> None:
    dur = max(0.05, min(dur_sec, 0.60))
    sample_rate = 48000
    amplitude = 0.12 * min(max(BEEP_PERCENT, 0.0), 100.0) / 100.0
    frame_count = int(sample_rate * dur)

    # Match volume.sh's PCM format and send the tone to Voice HAT, not ALSA default.
    with wave.open(BEEP_PATH, "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(4)
        wav.setframerate(sample_rate)
        frames = bytearray()
        for index in range(frame_count):
            value = int(
                2147483647 * amplitude * math.sin(2 * math.pi * freq * index / sample_rate)
            )
            frames.extend(struct.pack("<ii", value, value))
        wav.writeframes(frames)

    subprocess.run(
        ["aplay", "-D", BEEP_DEVICE, "-q", BEEP_PATH],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def warn_pattern() -> None:
    play_tone(520, 0.15)
    time.sleep(0.07)
    play_tone(520, 0.15)
    time.sleep(0.07)
    play_tone(360, 0.35)


def cancel_pattern() -> None:
    play_tone(880, 0.09)
    play_tone(660, 0.09)


def prompt_pattern() -> None:
    play_tone(880, 0.12)


def shutdown_pattern() -> None:
    play_tone(300, 0.25)
    play_tone(220, 0.35)


def main() -> int:
    subprocess.run(["pkill", "-f", f"^gpioset -c {GPIO_CHIP}"], check=False)

    print("AIY shutdown guard")
    print(f"- Button: {GPIO_CHIP}:{BUTTON_PIN} (active-low)")
    print(f"- LED:    {GPIO_CHIP}:{LED_PIN}")
    print(f"- Warn at: {WARN_SEC:.1f}s")
    print(f"- Shutdown at: {SHUTDOWN_SEC:.1f}s")

    led = LedController(GPIO_CHIP, LED_PIN)
    press_started_at = None
    warned = False
    shut = False

    try:
        led.set(False)
        last_pressed = read_button_pressed(GPIO_CHIP, BUTTON_PIN)

        while True:
            pressed = read_button_pressed(GPIO_CHIP, BUTTON_PIN)
            now = time.monotonic()

            if pressed and not last_pressed:
                press_started_at = now
                warned = False
                shut = False

            elif pressed and press_started_at is not None:
                held = now - press_started_at
                if held >= WARN_SEC and not warned:
                    warned = True
                    print("[warn] long-press detected, shutdown soon")
                    warn_pattern()
                    led.set(True)

                if held >= SHUTDOWN_SEC and warned and not shut:
                    shut = True
                    print("[shutdown] executing safe shutdown")
                    shutdown_pattern()
                    subprocess.run(["sudo", "/sbin/shutdown", "-h", "now"], check=False)

            elif (not pressed) and last_pressed:
                if press_started_at is not None:
                    held = now - press_started_at
                    if held <= SHORT_PRESS_MAX_SEC:
                        print("[prompt] short press")
                        prompt_pattern()
                    elif warned and held < SHUTDOWN_SEC:
                        print("[cancel] shutdown cancelled")
                        cancel_pattern()
                        led.set(False)
                press_started_at = None
                warned = False
                shut = False

            last_pressed = pressed
            time.sleep(POLL_SEC)

    except KeyboardInterrupt:
        print("\nExiting...")
    finally:
        led.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
