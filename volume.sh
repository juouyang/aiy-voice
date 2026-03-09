#!/usr/bin/env bash
set -euo pipefail

PLAY_PCT="${1:-60}"
MIC_GAIN="${2:-}"

if ! [[ "$PLAY_PCT" =~ ^[0-9]+$ ]]; then
  echo "Usage: $0 <playback 0-100> [mic_gain]" >&2
  exit 1
fi
if [ "$PLAY_PCT" -lt 0 ] || [ "$PLAY_PCT" -gt 100 ]; then
  echo "Playback must be 0-100" >&2
  exit 1
fi
if [ -n "$MIC_GAIN" ] && ! [[ "$MIC_GAIN" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "mic_gain must be numeric (e.g. 4.5)" >&2
  exit 1
fi

PLAY_GAIN=$(python3 - <<PY
pct=$PLAY_PCT
print(f"{pct/100:.2f}")
PY
)

{
  echo "export AIY_PLAYBACK_GAIN=$PLAY_GAIN"
  if [ -n "$MIC_GAIN" ]; then
    echo "export AIY_MIC_GAIN=$MIC_GAIN"
  fi
} > "$HOME/.aiy_volume.env"
chmod 600 "$HOME/.aiy_volume.env"

python3 - <<PY
import math, struct, wave, subprocess
sr=48000
secs=0.20
freq=660.0
pct=$PLAY_PCT
amp=0.12*(pct/100.0)
n=int(sr*secs)
path='/tmp/aiy_short_beep.wav'
with wave.open(path,'wb') as w:
    w.setnchannels(2)
    w.setsampwidth(4)
    w.setframerate(sr)
    frames=[]
    for i in range(n):
        v=int(2147483647*amp*math.sin(2*math.pi*freq*i/sr))
        frames.append(struct.pack('<ii', v, v))
    w.writeframes(b''.join(frames))
subprocess.run(['aplay','-D','hw:1,0','-q',path], check=False)
print(f"Playback set: {pct}% (AIY_PLAYBACK_GAIN={pct/100:.2f})")
PY

if [ -n "$MIC_GAIN" ]; then
  echo "Mic gain set: AIY_MIC_GAIN=$MIC_GAIN"
else
  echo "Mic gain unchanged. Pass second arg to set it."
fi
