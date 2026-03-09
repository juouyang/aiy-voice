#!/usr/bin/env bash
set -euo pipefail

PCT="${1:-60}"
if ! [[ "$PCT" =~ ^[0-9]+$ ]]; then
  echo "Usage: $0 <0-100>" >&2
  exit 1
fi
if [ "$PCT" -lt 0 ] || [ "$PCT" -gt 100 ]; then
  echo "Volume must be 0-100" >&2
  exit 1
fi

GAIN=$(python3 - <<PY
pct=$PCT
print(f"{pct/100:.2f}")
PY
)

cat > "$HOME/.aiy_volume.env" <<EOF
export AIY_PLAYBACK_GAIN=$GAIN
EOF

if [ -f "$HOME/aiy_button_record_play.py" ]; then
  python3 - <<PY
from pathlib import Path
import re
p=Path.home()/"aiy_button_record_play.py"
s=p.read_text()
pattern=r'PLAYBACK_GAIN\s*=\s*float\(os\.getenv\("AIY_PLAYBACK_GAIN",\s*"[0-9.]+"\)\)'
repl='PLAYBACK_GAIN = float(os.getenv("AIY_PLAYBACK_GAIN", "'+"$GAIN"+'"))'
s2=re.sub(pattern,repl,s)
if s2!=s:
    p.write_text(s2)
    print("Updated aiy_button_record_play.py default gain")
PY
fi

python3 - <<PY
import math, struct, wave, subprocess
sr=48000
secs=0.20
freq=660.0
pct=$PCT
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
print(f"Volume set to {pct}% (AIY_PLAYBACK_GAIN={pct/100:.2f})")
print('Played 0.2s test beep')
PY
