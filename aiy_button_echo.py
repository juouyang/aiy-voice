#!/usr/bin/env python3
import os
import re
import signal
import struct
import subprocess
import time
import wave
from pathlib import Path

BUTTON_PIN = int(os.getenv("AIY_BUTTON_PIN", "23"))
LED_PIN = int(os.getenv("AIY_LED_PIN", "25"))
SAMPLE_RATE = int(os.getenv("AIY_SAMPLE_RATE", "48000"))
CHANNELS = int(os.getenv("AIY_CHANNELS", "2"))
WAV_PATH = Path(os.getenv("AIY_WAV_PATH", "/tmp/aiy_test.wav"))
PLAY_WAV_PATH = Path(os.getenv("AIY_PLAY_WAV_PATH", "/tmp/aiy_test_play.wav"))
GPIO_CHIP = os.getenv("AIY_GPIO_CHIP", "gpiochip0")
AUDIO_FORMAT = os.getenv("AIY_AUDIO_FORMAT", "S32_LE")
MIC_GAIN = float(os.getenv("AIY_MIC_GAIN", "6.00"))
PLAYBACK_GAIN = float(os.getenv("AIY_PLAYBACK_GAIN", "0.60"))
TARGET_PEAK = float(os.getenv("AIY_TARGET_PEAK", "0.65"))
MAX_AUTO_GAIN = float(os.getenv("AIY_MAX_AUTO_GAIN", "12.0"))
SHORT_PRESS_MAX_SEC = float(os.getenv("AIY_SHORT_PRESS_MAX_SEC", "1.2"))
POLL_SEC = float(os.getenv("AIY_BUTTON_POLL_SEC", "0.02"))


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
        time.sleep(0.03)
        if self.proc.poll() is not None:
            print("[warn] LED gpio busy, check stale gpioset process")

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
    return subprocess.check_output(cmd, text=True).strip()


def detect_card(default: int = 1) -> int:
    env = os.getenv("AIY_CARD")
    if env and env.isdigit():
        return int(env)
    try:
        cards = Path("/proc/asound/cards").read_text()
    except OSError:
        return default
    for line in cards.splitlines():
        lower = line.lower()
        m = re.search(r"^(\s*\d+)\s+\[.*(google|sndrpigoogle|voice)", lower)
        if m:
            return int(m.group(1))
    return default


def read_button_pressed(chip: str, pin: int) -> bool:
    return run_output(["gpioget", "-c", chip, "--numeric", str(pin)]) == "0"


def stop_recorder(proc: subprocess.Popen | None) -> None:
    if proc and proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()


def play_tone(play_dev: str, freq: int, dur_sec: float = 0.10) -> None:
    dur = max(0.05, min(dur_sec, 0.50))
    subprocess.run(
        [
            "timeout",
            f"{dur:.2f}s",
            "speaker-test",
            "-D",
            play_dev,
            "-q",
            "-t",
            "sine",
            "-f",
            str(freq),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def beep_start(play_dev: str) -> None:
    play_tone(play_dev, 740, 0.08)
    play_tone(play_dev, 1040, 0.08)


def beep_stop(play_dev: str) -> None:
    play_tone(play_dev, 1040, 0.08)
    play_tone(play_dev, 740, 0.08)


def process_for_playback(src: Path, dst: Path) -> tuple[str, float, float]:
    with wave.open(str(src), "rb") as r:
        params = r.getparams()
        if params.sampwidth != 4:
            dst.write_bytes(src.read_bytes())
            return ("copy", 1.0, 1.0)
        frames = r.readframes(params.nframes)

    vals = struct.unpack("<" + "i" * (len(frames) // 4), frames)

    if params.nchannels == 2:
        left = vals[0::2]
        right = vals[1::2]
        l_rms = (sum(v * v for v in left) / max(len(left), 1)) ** 0.5
        r_rms = (sum(v * v for v in right) / max(len(right), 1)) ** 0.5
        mono = left if l_rms >= r_rms else right
        chosen = "L" if l_rms >= r_rms else "R"
    else:
        mono = vals
        chosen = "M"

    peak = max(abs(v) for v in mono) if mono else 1
    peak_pct = peak / 2147483647.0
    auto = TARGET_PEAK / peak_pct if peak_pct > 0 else 1.0
    auto = min(max(auto, 1.0), MAX_AUTO_GAIN)
    total_gain = MIC_GAIN * PLAYBACK_GAIN * auto

    scaled = []
    for v in mono:
        nv = int(v * total_gain)
        if nv > 2147483647:
            nv = 2147483647
        elif nv < -2147483648:
            nv = -2147483648
        scaled.append(nv)

    stereo = b"".join(struct.pack("<ii", v, v) for v in scaled)
    with wave.open(str(dst), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(4)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(stereo)

    return (chosen, peak_pct, auto)


def play_echo(play_dev: str, wav_path: Path, led: LedController) -> None:
    """Play the recorded audio while making the Echo state visible on the LED."""
    print("[echo] playback start")
    led_on = True
    led.set(led_on)
    try:
        player = subprocess.Popen(
            ["aplay", "-D", play_dev, "-q", str(wav_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        print(f"[warn] unable to start playback: {exc}")
        led.set(False)
        return

    try:
        next_toggle_at = time.monotonic() + 0.25
        while player.poll() is None:
            now = time.monotonic()
            if now >= next_toggle_at:
                led_on = not led_on
                led.set(led_on)
                next_toggle_at = now + 0.25
            time.sleep(0.02)
    finally:
        if player.poll() is None:
            player.terminate()
            try:
                player.wait(timeout=1)
            except subprocess.TimeoutExpired:
                player.kill()
        led.set(False)

    print("[echo] playback done")


def main() -> int:
    subprocess.run(["pkill", "-f", f"^gpioset -c {GPIO_CHIP}"], check=False)

    card = detect_card(default=1)
    cap_dev = os.getenv("AIY_CAP_DEV", f"hw:{card},0")
    play_dev = os.getenv("AIY_PLAY_DEV", f"plughw:{card},0")

    print("AIY button recording Echo")
    print(f"- Button: {GPIO_CHIP}:{BUTTON_PIN} (active-low)")
    print(f"- LED:    {GPIO_CHIP}:{LED_PIN}")
    print(f"- Capture device: {cap_dev}")
    print(f"- Playback device: {play_dev}")
    print(f"- Format: {AUDIO_FORMAT} {SAMPLE_RATE}Hz {CHANNELS}ch")
    print(f"- Mic gain: {MIC_GAIN:.2f}")
    print(f"- Playback gain: {PLAYBACK_GAIN:.2f}")
    print(
        "Short-press once to record; short-press again to Echo playback. Ctrl+C to exit."
    )

    led = LedController(GPIO_CHIP, LED_PIN)
    recorder = None
    recording = False

    try:
        led.set(False)
        last_pressed = read_button_pressed(GPIO_CHIP, BUTTON_PIN)
        press_started_at = None

        while True:
            pressed = read_button_pressed(GPIO_CHIP, BUTTON_PIN)
            now = time.monotonic()

            if pressed and not last_pressed:
                press_started_at = now

            elif (not pressed) and last_pressed:
                held = (now - press_started_at) if press_started_at else 0.0
                press_started_at = None
                if held <= SHORT_PRESS_MAX_SEC:
                    if not recording:
                        WAV_PATH.parent.mkdir(parents=True, exist_ok=True)
                        if WAV_PATH.exists():
                            WAV_PATH.unlink()
                        print("[rec] start")
                        beep_start(play_dev)
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
                        beep_stop(play_dev)

                        if WAV_PATH.exists() and WAV_PATH.stat().st_size > 44:
                            ch, peak_pct, auto = process_for_playback(
                                WAV_PATH, PLAY_WAV_PATH
                            )
                            print(
                                f"[proc] channel={ch} input_peak={peak_pct * 100:.2f}% auto_gain={auto:.2f}x"
                            )
                            play_echo(play_dev, PLAY_WAV_PATH, led)
                        else:
                            print("[warn] no valid audio recorded")
                            led.set(False)

            last_pressed = pressed
            time.sleep(POLL_SEC)

    except KeyboardInterrupt:
        print("\nExiting...")
    finally:
        stop_recorder(recorder)
        led.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
