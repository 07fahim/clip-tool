"""Find clip candidates in a stretch of a YouTube stream without Claude: grab the audio, take the loudest peaks,
transcribe 90 s around each, write '<stream time> text' lines to footage/<name>/lines.txt.
Usage: python scripts/scan_stream.py URL START END NAME   (START/END = stream time H:MM:SS)
Works on running lives and on ended "post_live" streams (see live_grab.py).
"""
import os
import re
import subprocess
import sys
from pathlib import Path

from live_grab import grab, to_seconds
from whisper_settings import WHISPER_PYTHON

PEAKS = 20

url, start, end, name = sys.argv[1:5]
folder = Path("footage") / name
windows = folder / "win"
windows.mkdir(parents=True, exist_ok=True)
audio = folder / "audio.m4a"
if not audio.exists():
    grab(url, start, end, audio, audio_only=True)
peaks = subprocess.run([sys.executable, "scripts/peaks.py", str(audio), str(PEAKS)],
                       capture_output=True, text=True, check=True).stdout
for clock in re.findall(r"^(\d+:\d\d:\d\d)", peaks, re.M):
    window_start = max(to_seconds(clock) - 60, 0)
    # window files are named by STREAM second, so lines.txt shows stream time
    wav = windows / f"w{to_seconds(start) + window_start}.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(window_start), "-t", "90", "-i", str(audio),
                    "-ac", "1", "-ar", "16000", str(wav)], check=True)
environment = dict(os.environ, HF_HOME="D:\\caches\\huggingface")
subprocess.run([WHISPER_PYTHON, "scripts/scan_windows.py", str(windows), str(folder / "lines.txt")],
               check=True, env=environment)
print(f"done: {folder / 'lines.txt'}")
