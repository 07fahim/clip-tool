"""Write the words the first transcription dropped: speech that the detector hears but that has no caption word.

Run in the whisper env (D:/Tools/whisper-env):
  python scripts/recover_words.py PICK.mp4 PICK.words.json PICK.speech.json OUT.json
Writes [[start, end, word], ...] in seconds for the uncovered stretches only.
"""
import argparse
import json
import os
from pathlib import Path

from caption_timing import is_hallucination, keep_segment, uncovered_speech

SAMPLE_RATE = 16000
# audio on each side of a stretch gives Whisper the start and end of the sentence; words outside the stretch are not kept
CONTEXT_SECONDS = 0.3


def recover(video: str, words: list, speech: list) -> list:
    from faster_whisper import WhisperModel
    from faster_whisper.audio import decode_audio

    from whisper_settings import WHISPER_MODEL

    audio = decode_audio(video, sampling_rate=SAMPLE_RATE)
    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    recovered = []
    for start, end in uncovered_speech(words, speech):
        offset = max(start - CONTEXT_SECONDS, 0)
        chunk = audio[int(offset * SAMPLE_RATE):int((end + CONTEXT_SECONDS) * SAMPLE_RATE)]
        # the filters that dropped this speech the first time are off; keep_segment drops what is not speech after all
        segments, _ = model.transcribe(chunk, language="en", word_timestamps=True, vad_filter=False, beam_size=5,
                                       condition_on_previous_text=False, no_speech_threshold=None, log_prob_threshold=None)
        for segment in segments:
            if not keep_segment(segment.no_speech_prob, segment.avg_logprob) or is_hallucination(segment.text):
                continue
            for word in segment.words:
                middle = offset + (word.start + word.end) / 2
                if start <= middle <= end and word.word.strip():
                    recovered.append([round(offset + word.start, 2), round(offset + word.end, 2), word.word.strip()])
    return recovered


if __name__ == "__main__":
    # arguments: the video, its words file, its speech file, then the json file to write
    parser = argparse.ArgumentParser(allow_abbrev=False, description="Transcribe speech the first pass dropped")
    parser.add_argument("video")
    parser.add_argument("words")
    parser.add_argument("speech")
    parser.add_argument("output")
    arguments = parser.parse_args()
    found = recover(arguments.video, json.loads(Path(arguments.words).read_text(encoding="utf8")),
                    json.loads(Path(arguments.speech).read_text(encoding="utf8")))
    # a stopped run must not leave a half file that looks finished
    part = Path(arguments.output + ".part")
    part.write_text(json.dumps(found), encoding="utf8")
    os.replace(part, arguments.output)
    print(len(found), "words recovered")
