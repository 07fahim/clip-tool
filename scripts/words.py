"""Word-level transcript for caption sync.
Run with the whisper env (has faster-whisper):
  D:/Tools/whisper-env/Scripts/python scripts/words.py SRC.mp4 OUT.json
Writes [[start, end, word], ...] in seconds.
"""
import json
import sys

from faster_whisper import WhisperModel

from whisper_settings import WHISPER_BEAM_SIZE, WHISPER_MODEL, WHISPER_PROMPT

src, out = sys.argv[1], sys.argv[2]
model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
segments, _ = model.transcribe(src, word_timestamps=True, vad_filter=True, initial_prompt=WHISPER_PROMPT, beam_size=WHISPER_BEAM_SIZE)
words = [[round(w.start, 2), round(w.end, 2), w.word.strip()] for s in segments for w in s.words]
json.dump(words, open(out, "w", encoding="utf8"))
print(len(words), "words")
