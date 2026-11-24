WHISPER_PYTHON = "D:/Tools/whisper-env/Scripts/python.exe"
# captions (word timestamps): small. Timed 2026-11-23 on 60 s of real speech, CPU int8, greedy search: tiny 4 s, base 6 s,
# small 18 s; medium was about 2.7x slower than small on 2026-11-22 and the GTX 1050 run was far slower still (stopped
# after minutes), so a 3 h stream scan took 35 min with medium
WHISPER_MODEL = "small"
# scanning only has to read what was said around a loud moment, so the fastest readable model is used
SCAN_WHISPER_MODEL = "base"
# greedy search (the timings above); the library default of 5 tries more candidates per word and is slower
WHISPER_BEAM_SIZE = 1
# names Whisper mishears without a hint (small heard "I'm losing it" for "I'm Lucia"); add a name when one is missed.
# Names only: with a game sentence in it ("Tomo plays GTA 6 as Lucia and Jason") Whisper small echoed the hint instead of
# the speech on two Fortnite clips (1 and 4 words instead of about 75, measured 2026-11-23); the names-only list gave 76 and 72
WHISPER_PROMPT = "IShowSpeed. Kai Cenat. Tomo. Lucia. Jason."
# Whisper model downloads go to D: because C: is small
HF_HOME = "D:/caches/huggingface"
