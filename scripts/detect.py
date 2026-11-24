"""Find clip candidates in a source video: download its audio, take the loudest moments, transcribe 90 s around each.

Writes footage/sources/<id>/candidates.json. Run again after a stop: finished steps are not repeated.
"""
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

from job_locks import COMPUTE_LOCK, DOWNLOAD_LOCK
from live_grab import YT_DLP
from peaks import loudest_moments
from sources import SOURCES_DIR, load_sources, parse_lines, save_source, write_json
from whisper_settings import HF_HOME, WHISPER_PROMPT, WHISPER_PYTHON

logger = logging.getLogger("detect")

PEAK_COUNT = 20
# a download or a Whisper run that has not ended after an hour is stuck; the audio of a 3 h stream takes minutes
JOB_TIMEOUT_SECONDS = 3600
WINDOW_SECONDS = 90
# the window starts this long before the peak so the setup of the moment is in it (the loudest second is the payoff)
LEAD_SECONDS = 60
AUDIO_NAME = "audio.m4a"
LINES_NAME = "lines.txt"
# moments viewers pointed at in the comments, filled in by hand or through vidIQ: [{"second": 2791, "likes": 31, "text": "..."}]
COMMENTS_NAME = "comment_moments.json"
# a moment nobody measured gets a loudness far below any speech or game sound (LUFS), so only its likes rank it
UNMEASURED_LOUDNESS = -70.0
# HLS first: plain https audio is sometimes refused with a 403 and was throttled to about 30 KiB/s, HLS ran 7 times faster
# (probed 2026-11-23); the later choices keep a video that has no HLS audio
AUDIO_FORMAT = "ba[protocol^=m3u8]/bestaudio[ext=m4a]/bestaudio"
# a window with fewer spoken words than this is music, a crowd or credits rather than a moment someone can react to
# (the credits line "Thanks for watching!" has 3); shortcut: a guess, raise it if music with lyrics still ranks high
MIN_SPEECH_WORDS = 5
# on music or silence Whisper often prints its own name hint back as a line; shortcut: a real line made only of hint
# words (for example just "Lucia!") is dropped too
HINT_WORDS = set(re.findall(r"[a-z0-9]+", WHISPER_PROMPT.lower()))


# YouTube answered about 15 downloads in an hour from this PC with "429 Too Many Requests" and then a bot check, for
# every request, for a while (seen 2026-11-23). Waiting and trying again is the only thing that helps; the seconds
# below are a guess
RATE_LIMIT_MARKERS = ("HTTP Error 429", "not a bot")
RATE_LIMIT_WAITS = (60, 180)


class RateLimited(RuntimeError):
    pass


def run_command(command: list, env=None, timeout: int = JOB_TIMEOUT_SECONDS) -> None:
    for wait in (*RATE_LIMIT_WAITS, None):
        result = subprocess.run(command, env=env, timeout=timeout, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace")
        # the output is kept for the server window: the last lines say what the program complained about
        print(result.stdout[-2000:])
        if result.returncode == 0:
            return
        if not any(marker in result.stdout for marker in RATE_LIMIT_MARKERS):
            raise subprocess.CalledProcessError(result.returncode, command, output=result.stdout)
        if wait is None:
            raise RateLimited("YouTube is limiting downloads from this PC (429 or a bot check); wait about 30 minutes and try again")
        logger.warning(f"YouTube rate limit; trying again in {wait} s")
        time.sleep(wait)


def is_hint_echo(text: str) -> bool:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return bool(words) and all(word in HINT_WORDS for word in words)


def rank_candidates(candidates: list) -> list:
    # speech first, then the moment viewers liked most in the comments, then the louder one; rank 1 is the best.
    # The list keeps its time order, the page sorts by rank
    best_first = sorted(candidates, key=lambda candidate: (candidate["words"] >= MIN_SPEECH_WORDS, candidate.get("comment_likes", 0),
                                                           candidate["loudness"]), reverse=True)
    for rank, candidate in enumerate(best_first, 1):
        candidate["rank"] = rank
    return candidates


def load_comment_moments(folder: Path, duration: int = 0) -> list:
    path = folder / COMMENTS_NAME
    if not path.is_file():
        return []
    moments = json.loads(path.read_text(encoding="utf8"))
    return [moment for moment in moments if isinstance(moment.get("second"), int) and isinstance(moment.get("likes"), int)
            and 0 <= moment["second"] and (not duration or moment["second"] < duration)]


def covers(peak_second: int, second: int) -> bool:
    start = max(peak_second - LEAD_SECONDS, 0)
    return start <= second <= start + WINDOW_SECONDS


def add_comment_peaks(peaks: list, moments: list) -> list:
    # a liked moment that no loudness peak's window already holds gets a window of its own
    added = list(peaks)
    for moment in sorted(moments, key=lambda moment: -moment["likes"]):
        if not any(covers(peak_second, moment["second"]) for peak_second, _ in added):
            added.append((moment["second"], UNMEASURED_LOUDNESS))
    return added


def mark_comment_likes(candidates: list, moments: list) -> list:
    for candidate in candidates:
        inside = [moment for moment in moments if candidate["start"] <= moment["second"] <= candidate["end"]]
        candidate["comment_likes"] = sum(moment["likes"] for moment in inside)
        if inside:
            candidate["comment"] = max(inside, key=lambda moment: moment["likes"]).get("text", "")
    return candidates


def build_candidates(windows: list, peaks: list, duration: int = 0) -> list:
    by_offset = {window["offset"]: window for window in windows}
    candidates = []
    for second, lufs in peaks:
        start = max(second - LEAD_SECONDS, 0)
        lines = by_offset.get(start, {"lines": []})["lines"]
        text = " ".join(spoken for _, spoken in lines if not is_hint_echo(spoken))
        candidates.append({"id": f"c{start}", "peak": second, "loudness": lufs, "start": start,
                           "end": min(start + WINDOW_SECONDS, duration) if duration else start + WINDOW_SECONDS, "text": text,
                           "words": len(text.split())})
    return candidates


def download_audio(source: dict, audio: Path, runner) -> None:
    if audio.exists():
        return
    command = [YT_DLP, "--js-runtimes", "node", "--no-playlist", "-f", AUDIO_FORMAT, "-N", "4", "--retries", "5",
               "--fragment-retries", "10", "-o", str(audio), "--", source["url"]]
    try:
        runner(command)
    except BaseException:
        # a half-written file must not count as done on the next run
        audio.unlink(missing_ok=True)
        raise


def cut_windows(audio: Path, windows_dir: Path, peaks: list, runner) -> int:
    windows_dir.mkdir(exist_ok=True)
    cut = 0
    for second, _ in peaks:
        start = max(second - LEAD_SECONDS, 0)
        wav = windows_dir / f"w{start}.wav"
        if wav.exists():
            continue
        # cut beside the final name and swap, so a stopped run never leaves a half window that looks finished
        part = wav.with_name(wav.name + ".part")
        try:
            runner(["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-t", str(WINDOW_SECONDS), "-i", str(audio),
                    "-ac", "1", "-ar", "16000", "-f", "wav", str(part)])
            os.replace(part, wav)
            cut += 1
        finally:
            part.unlink(missing_ok=True)
    return cut


def transcribe(windows_dir: Path, lines: Path, runner) -> None:
    if lines.exists():
        return
    # scan_windows writes window by window; only a finished file may be named lines.txt, or a stopped run is trusted
    part = lines.with_name(lines.name + ".part")
    runner([WHISPER_PYTHON, "scripts/scan_windows.py", str(windows_dir), str(part)], env=dict(os.environ, HF_HOME=HF_HOME))
    os.replace(part, lines)


def scan_source(source_id: str, root: Path = SOURCES_DIR, runner=run_command, find_peaks=loudest_moments) -> list:
    source = next(source for source in load_sources(root) if source["id"] == source_id)
    folder = root / source_id
    audio, lines = folder / AUDIO_NAME, folder / LINES_NAME
    logger.info(f"scanning '{source_id}': {source['title']}")
    try:
        with DOWNLOAD_LOCK:
            download_audio(source, audio, runner)
        with COMPUTE_LOCK:
            moments = load_comment_moments(folder, source["duration"])
            peaks = add_comment_peaks(find_peaks(str(audio), PEAK_COUNT), moments)
            # a transcript made before these windows existed does not hold them, so it is made again
            if cut_windows(audio, folder / "win", peaks, runner):
                lines.unlink(missing_ok=True)
            transcribe(folder / "win", lines, runner)
            built = build_candidates(parse_lines(lines.read_text(encoding="utf8")), peaks, source["duration"])
            candidates = rank_candidates(mark_comment_likes(built, moments))
    except BaseException:
        save_source(dict(source, status="failed"), root)
        raise
    write_json(folder / "candidates.json", candidates)
    save_source(dict(source, status="scanned"), root)
    logger.info(f"'{source_id}': [{len(candidates)}] candidates")
    return candidates


def load_candidates(source_id: str, root: Path = SOURCES_DIR) -> list:
    path = root / source_id / "candidates.json"
    return json.loads(path.read_text(encoding="utf8")) if path.is_file() else []


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    # argument: the source id from footage/sources (made by the page's Sources tab)
    parser = argparse.ArgumentParser(allow_abbrev=False, description="Find clip candidates in one source video")
    parser.add_argument("source_id", help="source id, for example v_uehmwf9viss")
    scan_source(parser.parse_args().source_id)
