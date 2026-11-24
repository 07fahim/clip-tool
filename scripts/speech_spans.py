"""Find where a person is speaking in a video: the seconds that the speech detector (the one inside faster-whisper) hears.

Run in the whisper env (D:/Tools/whisper-env). Writes a list of [start, end] seconds, used to end a caption where the voice stops.
"""
import argparse
import json
import os
from pathlib import Path

from faster_whisper.audio import decode_audio
from faster_whisper.vad import VadOptions, get_speech_timestamps

SAMPLE_RATE = 16000
# a pause shorter than this does not split speech; the caption grouping handles pauses on its own
MIN_SILENCE_MS = 300


def find_speech(video: str) -> list:
    audio = decode_audio(video, sampling_rate=SAMPLE_RATE)
    spans = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=MIN_SILENCE_MS))
    return [[round(span["start"] / SAMPLE_RATE, 2), round(span["end"] / SAMPLE_RATE, 2)] for span in spans]


if __name__ == "__main__":
    # arguments: the video, then the json file to write
    parser = argparse.ArgumentParser(allow_abbrev=False, description="Write the speech spans of a video")
    parser.add_argument("video", help="the pick mp4")
    parser.add_argument("output", help="the .speech.json to write")
    arguments = parser.parse_args()
    # a stopped run must not leave a half file that looks finished
    part = Path(arguments.output + ".part")
    part.write_text(json.dumps(find_speech(arguments.video)), encoding="utf8")
    os.replace(part, arguments.output)
