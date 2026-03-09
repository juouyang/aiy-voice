#!/usr/bin/env python3
import base64
import json
import os
import re
import signal
import struct
import subprocess
import time
import wave
from datetime import datetime
from pathlib import Path

from openai import OpenAI

BUTTON_PIN = int(os.getenv('AIY_BUTTON_PIN', '23'))
LED_PIN = int(os.getenv('AIY_LED_PIN', '25'))
GPIO_CHIP = os.getenv('AIY_GPIO_CHIP', 'gpiochip0')

REC_RATE = 48000
REC_CH = 2
REC_FMT = 'S32_LE'
RAW_WAV = Path('/tmp/aiy_input_raw.wav')
IN_WAV = Path('/tmp/aiy_input_onecall.wav')
OUT_WAV = Path('/tmp/aiy_reply.wav')
LOG_PATH = Path(os.getenv('AIY_LOG_PATH', str(Path.home() / 'logs' / 'assistant.log')))

MIC_GAIN = float(os.getenv('AIY_MIC_GAIN', '6.0'))
PLAYBACK_GAIN = float(os.getenv('AIY_PLAYBACK_GAIN', '0.33'))
TARGET_PEAK = float(os.getenv('AIY_TARGET_PEAK', '0.70'))
MAX_AUTO_GAIN = float(os.getenv('AIY_MAX_AUTO_GAIN', '8.0'))

ONECALL_MODEL = os.getenv('AIY_ONECALL_MODEL', 'gpt-4o-audio-preview')
TTS_MODEL = os.getenv('AIY_TTS_MODEL', 'gpt-4o-mini-tts')
TTS_VOICE = os.getenv('AIY_TTS_VOICE', 'alloy')

SYSTEM_PROMPT = (
    '你是給小孩使用的語音助理。'
    '請用繁體中文、短句、溫和語氣回答。'
    '避免危險、暴力、成人內容。'
    '若問題不清楚，先問一個簡短澄清問題。'
)

TEXT_FORMAT_PROMPT = (
    '請另外提供文字輸出，使用兩行格式：\n'
    'HEARD: <你辨識到的使用者原話>\n'
    'REPLY: <你要回覆的內容>'
)


class LedController:
    def __init__(self, chip: str, pin: int):
        self.chip = chip
        self.pin = pin
        self.proc = None

    def set(self, on: bool) -> None:
        self._stop_holder()
        value = '1' if on else '0'
        self.proc = subprocess.Popen(
            ['gpioset', '-c', self.chip, f'{self.pin}={value}'],
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
    return subprocess.check_output(cmd, text=True).strip()


def detect_card(default: int = 1) -> int:
    env = os.getenv('AIY_CARD')
    if env and env.isdigit():
        return int(env)
    try:
        cards = Path('/proc/asound/cards').read_text()
    except OSError:
        return default
    for line in cards.splitlines():
        m = re.search(r'^(\s*\d+)\s+\[.*(google|sndrpigoogle|voice)', line.lower())
        if m:
            return int(m.group(1))
    return default


def read_button_pressed(chip: str, pin: int) -> bool:
    return run_output(['gpioget', '-c', chip, '--numeric', str(pin)]) == '0'


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


def _resample_int16(samples: list[int], src_rate: int, dst_rate: int) -> list[int]:
    if src_rate == dst_rate or not samples:
        return samples
    out_len = max(1, int(len(samples) * dst_rate / src_rate))
    out = []
    for i in range(out_len):
        src_idx = int(i * src_rate / dst_rate)
        if src_idx >= len(samples):
            src_idx = len(samples) - 1
        out.append(samples[src_idx])
    return out


def preprocess_input_audio(src: Path, dst: Path) -> tuple[float, float, str]:
    with wave.open(str(src), 'rb') as r:
        ch = r.getnchannels()
        sw = r.getsampwidth()
        fr = r.getframerate()
        data = r.readframes(r.getnframes())

    if sw != 4:
        raise RuntimeError('unexpected sample width')

    vals = struct.unpack('<' + 'i' * (len(data) // 4), data)
    if ch == 2:
        left = vals[0::2]
        right = vals[1::2]
        l_rms = (sum(v * v for v in left) / max(len(left), 1)) ** 0.5
        r_rms = (sum(v * v for v in right) / max(len(right), 1)) ** 0.5
        mono32 = left if l_rms >= r_rms else right
        channel = 'L' if l_rms >= r_rms else 'R'
    else:
        mono32 = vals
        channel = 'M'

    peak = max(abs(v) for v in mono32) if mono32 else 1
    peak_pct = peak / 2147483647.0
    auto = TARGET_PEAK / peak_pct if peak_pct > 0 else 1.0
    auto = min(max(auto, 1.0), MAX_AUTO_GAIN)
    total_gain = MIC_GAIN * auto

    mono16 = []
    for v in mono32:
        nv = int(v * total_gain)
        nv = max(min(nv, 2147483647), -2147483648)
        s16 = nv >> 16
        s16 = max(min(s16, 32767), -32768)
        mono16.append(s16)

    mono16 = _resample_int16(mono16, fr, 16000)

    raw = b''.join(struct.pack('<h', s) for s in mono16)
    with wave.open(str(dst), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(raw)

    return peak_pct, auto, channel


def fix_streamed_wav_header(path: Path) -> None:
    try:
        b = bytearray(path.read_bytes())
    except Exception:
        return
    if len(b) < 44 or b[0:4] != b'RIFF' or b[8:12] != b'WAVE':
        return
    if b[4:8] == b'\xff\xff\xff\xff':
        b[4:8] = int(len(b) - 8).to_bytes(4, 'little', signed=False)
    idx = b.find(b'data')
    if idx != -1 and idx + 8 <= len(b) and b[idx + 4:idx + 8] == b'\xff\xff\xff\xff':
        b[idx + 4:idx + 8] = int(len(b) - (idx + 8)).to_bytes(4, 'little', signed=False)
    try:
        path.write_bytes(bytes(b))
    except Exception:
        return


def attenuate_wav_s16(path: Path, gain: float) -> None:
    gain = max(0.0, min(gain, 1.0))
    if gain >= 0.999:
        return
    fix_streamed_wav_header(path)
    try:
        with wave.open(str(path), 'rb') as r:
            ch = r.getnchannels()
            sw = r.getsampwidth()
            fr = r.getframerate()
            nframes = r.getnframes()
            if nframes <= 0:
                return
            data = r.readframes(nframes)
    except Exception:
        return
    if sw != 2:
        return
    vals = struct.unpack('<' + 'h' * (len(data) // 2), data)
    scaled = [max(min(int(v * gain), 32767), -32768) for v in vals]
    out = b''.join(struct.pack('<h', x) for x in scaled)
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(ch)
        w.setsampwidth(sw)
        w.setframerate(fr)
        w.writeframes(out)


def parse_text_log(raw_text: str) -> tuple[str, str]:
    heard = ''
    reply = ''
    for line in raw_text.splitlines():
        s = line.strip()
        if s.upper().startswith('HEARD:'):
            heard = s.split(':', 1)[1].strip()
        elif s.upper().startswith('REPLY:'):
            reply = s.split(':', 1)[1].strip()
    if not reply:
        reply = raw_text.strip()
    return heard, reply


def append_log(heard: str, reply: str, raw_text: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        'ts': datetime.now().isoformat(timespec='seconds'),
        'heard': heard,
        'reply': reply,
        'raw_text': raw_text,
    }
    with LOG_PATH.open('a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')


def synth_reply_tts(client: OpenAI, text: str, out_wav: Path) -> None:
    if not text:
        raise RuntimeError('Empty reply text for TTS')
    with client.audio.speech.with_streaming_response.create(
        model=TTS_MODEL,
        voice=TTS_VOICE,
        input=text,
        response_format='wav',
    ) as resp:
        resp.stream_to_file(str(out_wav))


def onecall_audio_reply(client: OpenAI, in_wav: Path, out_wav: Path) -> tuple[str, str]:
    audio_b64 = base64.b64encode(in_wav.read_bytes()).decode('ascii')

    # One API call for speech understanding + text response
    resp = client.chat.completions.create(
        model=ONECALL_MODEL,
        messages=[
            {'role': 'system', 'content': SYSTEM_PROMPT + '\n' + TEXT_FORMAT_PROMPT},
            {
                'role': 'user',
                'content': [
                    {
                        'type': 'input_audio',
                        'input_audio': {
                            'data': audio_b64,
                            'format': 'wav',
                        },
                    }
                ],
            },
        ],
        temperature=0.4,
    )

    msg = resp.choices[0].message
    raw_text = (msg.content or '').strip()
    heard, reply = parse_text_log(raw_text)
    if not reply:
        reply = raw_text

    # Separate TTS call guarantees spoken content is reply-only.
    synth_reply_tts(client, reply, out_wav)
    attenuate_wav_s16(out_wav, PLAYBACK_GAIN)

    append_log(heard, reply, raw_text)
    return heard, reply


def main() -> int:
    if 'OPENAI_API_KEY' not in os.environ:
        print('Missing OPENAI_API_KEY. source ~/.aiy_openai.env first')
        return 1

    subprocess.run(['pkill', '-f', f'^gpioset -c {GPIO_CHIP}'], check=False)

    card = detect_card(default=1)
    cap_dev = os.getenv('AIY_CAP_DEV', f'hw:{card},0')
    play_dev = os.getenv('AIY_PLAY_DEV', f'plughw:{card},0')

    client = OpenAI()
    led = LedController(GPIO_CHIP, LED_PIN)
    recorder = None
    recording = False

    print('AIY one-call cloud assistant (PTT)')
    print(f'- Capture: {cap_dev}')
    print(f'- Playback: {play_dev}')
    print(f'- Model: {ONECALL_MODEL}')
    print(f'- TTS: {TTS_MODEL} (voice={TTS_VOICE})')
    print(f'- Playback gain: {PLAYBACK_GAIN:.2f}')
    print(f'- Log: {LOG_PATH}')
    print('Hold button to record, release to process and reply.')

    try:
        led.set(False)
        last_pressed = read_button_pressed(GPIO_CHIP, BUTTON_PIN)

        while True:
            pressed = read_button_pressed(GPIO_CHIP, BUTTON_PIN)

            if pressed and not last_pressed and not recording:
                if RAW_WAV.exists():
                    RAW_WAV.unlink()
                print('[rec] start')
                led.set(True)
                recorder = subprocess.Popen([
                    'arecord', '-D', cap_dev, '-f', REC_FMT,
                    '-r', str(REC_RATE), '-c', str(REC_CH), '-q', str(RAW_WAV)
                ])
                recording = True

            elif (not pressed) and last_pressed and recording:
                print('[rec] stop')
                stop_recorder(recorder)
                recorder = None
                recording = False

                if not RAW_WAV.exists() or RAW_WAV.stat().st_size <= 44:
                    print('[warn] no valid audio')
                    led.set(False)
                    last_pressed = pressed
                    time.sleep(0.02)
                    continue

                try:
                    peak_pct, auto, channel = preprocess_input_audio(RAW_WAV, IN_WAV)
                    print(f'[prep] channel={channel} input_peak={peak_pct*100:.1f}% auto={auto:.2f}x')
                    print('[onecall-text] ...')
                    heard, reply = onecall_audio_reply(client, IN_WAV, OUT_WAV)
                    if heard:
                        print(f'[heard] {heard}')
                    print(f'[reply] {reply}')
                    print('[play] ...')
                    subprocess.run(['aplay', '-D', play_dev, '-q', str(OUT_WAV)], check=False)
                    print('[done]')
                except Exception as e:
                    print(f'[error] {e}')
                finally:
                    led.set(False)

            last_pressed = pressed
            time.sleep(0.02)

    except KeyboardInterrupt:
        print('\nExiting...')
    finally:
        stop_recorder(recorder)
        led.close()

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
