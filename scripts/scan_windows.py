"""Transcribe every .wav in a folder (one model load) into one text file, for picking moments.
File names are w<offset seconds>.wav; lines are '<offset + segment start> text'.
Usage (whisper env): python scripts/scan_windows.py WIN_DIR OUT.txt
"""
import sys
from pathlib import Path

from faster_whisper import WhisperModel

from whisper_settings import SCAN_WHISPER_MODEL, WHISPER_BEAM_SIZE, WHISPER_PROMPT

model = WhisperModel(SCAN_WHISPER_MODEL, device="cpu", compute_type="int8")
with open(sys.argv[2], "w", encoding="utf8") as out:
    for wav in sorted(Path(sys.argv[1]).glob("w*.wav"), key=lambda p: int(p.stem[1:])):
        offset = int(wav.stem[1:])
        out.write(f"=== {offset // 3600}:{offset % 3600 // 60:02}:{offset % 60:02}\n")
        for seg in model.transcribe(str(wav), vad_filter=True, initial_prompt=WHISPER_PROMPT, beam_size=WHISPER_BEAM_SIZE)[0]:
            t = offset + int(seg.start)
            out.write(f"{t // 3600}:{t % 3600 // 60:02}:{t % 60:02} {seg.text.strip()}\n")
        out.flush()
