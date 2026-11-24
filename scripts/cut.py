"""Turn an approved candidate into a clip: download its section, add it to the library, render it, queue it."""
import logging
import math
import os
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path

import facebook_queue
import queue_store
from camera import find_clip_crop
from clip_store import MAX_TITLE_LENGTH, PICKS, add_clip, build_text, is_number, is_plain_name, load_clips, load_presets
from detect import WINDOW_SECONDS, run_command
from facebook_schedule import DEFAULT_QUEUE as FACEBOOK_QUEUE
from job_locks import COMPUTE_LOCK, DOWNLOAD_LOCK
from live_grab import YT_DLP
from make_gta_clips import make_clip
from queue_store import DHAKA
from youtube_schedule import DEFAULT_QUEUE as YOUTUBE_QUEUE

logger = logging.getLogger("cut")

# under 5 s is a misclick; a clip stays inside one scan window (the section download is cut from it); platform limits
# for reels longer than 60 s are NOT verified (2026-11-24), so check them before posting a clip that long
MIN_CLIP_SECONDS, MAX_CLIP_SECONDS = 5, WINDOW_SECONDS
# a downloaded section must hold at least this share of the video it was asked for (a good one measured 96 to 100%)
MIN_VIDEO_SHARE = 0.9
# only the chosen seconds are downloaded, with this much extra on each side so a cut never starts or ends mid-word
CUT_PADDING_SECONDS = 2
# Facebook needs a reel more than 10 minutes ahead, so the first slot is looked for from 15 minutes from now
QUEUE_LEAD = timedelta(minutes=15)
# two clicks at once would both read the queue before either saved it
QUEUE_LOCK = threading.Lock()


def check_approval(approval: dict, candidate: dict) -> list:
    problems = []
    if not is_plain_name(approval.get("name")):
        problems.append("name: letters, digits, _ and - only")
    title = approval.get("title")
    if not isinstance(title, str) or not 1 <= len(title) <= MAX_TITLE_LENGTH:
        problems.append(f"title: 1 to {MAX_TITLE_LENGTH} characters")
    start, end = approval.get("from"), approval.get("to")
    window = candidate["end"] - candidate["start"]
    if not (is_number(start) and is_number(end) and 0 <= start < end <= window
            and MIN_CLIP_SECONDS <= end - start <= MAX_CLIP_SECONDS):
        problems.append(f"from and to: seconds inside the 0 to {window} s window, {MIN_CLIP_SECONDS} to {MAX_CLIP_SECONDS} s long")
    return problems


# HLS first: the https formats get a 403 when ffmpeg cuts a section; the later choices keep a video that has no HLS
SECTION_FORMAT = "bv*[protocol^=m3u8][height<=1080]+ba[protocol^=m3u8]/bv*[height<=1080]+ba/b[height<=1080]"


def pick_range(candidate: dict, approval: dict) -> tuple:
    # seconds inside the candidate window: the chosen range plus the padding, kept inside the window
    window = candidate["end"] - candidate["start"]
    return (max(math.floor(approval["from"]) - CUT_PADDING_SECONDS, 0),
            min(math.ceil(approval["to"]) + CUT_PADDING_SECONDS, window))


def section_command(source: dict, candidate: dict, rel_range: tuple, output: Path) -> list:
    # forced keyframes make the cut exact; without them it snaps to a keyframe up to 2 s early and the segments drift
    return [YT_DLP, "--js-runtimes", "node", "--no-playlist", "--download-sections",
            f"*{candidate['start'] + rel_range[0]}-{candidate['start'] + rel_range[1]}",
            "--force-keyframes-at-cuts", "-f", SECTION_FORMAT, "-N", "4", "--retries", "5", "--fragment-retries", "10",
            "--merge-output-format", "mp4", "-o", str(output), "--", source["url"]]


def video_seconds(path: Path) -> float:
    shown = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=duration", "-of", "csv=p=0",
                            str(path)], capture_output=True, text=True).stdout.strip()
    return float(shown) if shown else 0.0


def download_pick(source: dict, candidate: dict, rel_range: tuple, pick: str, runner) -> Path:
    output, temporary = PICKS / f"{pick}.mp4", PICKS / f"{pick}.part.mp4"
    if output.exists():
        # the same range of the same moment is the same section; clips cut from it may be rendering from this file
        return output
    try:
        runner(section_command(source, candidate, rel_range, temporary))
        # seen 2026-11-24: yt-dlp said success on sections whose video held 5 to 30 s of a 62 s window; the render was black
        wanted, got = rel_range[1] - rel_range[0], video_seconds(temporary)
        if got < wanted * MIN_VIDEO_SHARE:
            raise RuntimeError(f"the download is short: {got:.0f} s of video in a {wanted} s section; try again")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def approve_candidate(source: dict, candidate: dict, approval: dict, runner=run_command, render=make_clip,
                      find_crop=find_clip_crop) -> dict:
    problems = check_approval(approval, candidate)
    presets = load_presets()
    look = presets.get("creators", {}).get(source["creator"])
    if look is None:
        problems.append(f"creator: '{source['creator']}' is not in the Creators list")
    if problems:
        raise ValueError("; ".join(problems))
    rel_range = pick_range(candidate, approval)
    # the range is part of the name: another range of the same moment is another file
    pick = f"{source['id']}_{candidate['id']}_{rel_range[0]}_{rel_range[1]}"
    existed = (PICKS / f"{pick}.mp4").exists()
    with DOWNLOAD_LOCK:
        output = download_pick(source, candidate, rel_range, pick, runner)
    with COMPUTE_LOCK:
        return add_and_render(source, candidate, approval, rel_range, pick, output, existed, look, presets, find_crop, render)


def add_and_render(source: dict, candidate: dict, approval: dict, rel_range: tuple, pick: str, output: Path, existed: bool,
                   look: dict, presets: dict, find_crop, render) -> dict:
    clip = {"name": approval["name"], "streamer": source["creator"], "pick": pick, "title": approval["title"],
            "bottom_line": look.get("bottom_line", ""),
            "segments": [[approval["from"] - rel_range[0], approval["to"] - rel_range[0]]], "caption": look["caption"],
            "layout": look["layout"], "hook": approval.get("hook", ""), "body": approval.get("body", ""), "extra_tags": []}
    if not clip["hook"]:
        del clip["hook"]
    # cams move within a stream, so the crop is looked for in the middle of the clip; none found keeps the layout's own
    default_crop = presets["layouts"].get(look["layout"], {}).get("cam_crop")
    found_crop = find_crop(output, (approval["from"] + approval["to"]) / 2 - rel_range[0], default_crop) if default_crop else None
    if found_crop:
        clip["cam_crop"] = found_crop
    elif default_crop:
        # the layout's own crop may show the wrong part; the Library asks the owner to look at the frame
        clip["cam_unverified"] = True
    problems = add_clip(clip)
    if problems:
        # nothing in the library uses this download, so it must not pile up
        if not existed:
            output.unlink(missing_ok=True)
        raise ValueError("; ".join(problems))
    render(clip)
    logger.info(f"'{clip['name']}': cut from '{source['id']}' {candidate['start']}-{candidate['end']} and rendered")
    return clip


def next_slot(queue_path: Path) -> str:
    queue = queue_store.load_queue(queue_path) if queue_path.exists() else []
    return queue_store.suggest_slots(queue, 1, now=datetime.now(DHAKA) + QUEUE_LEAD)[0]


def in_queue(queue_path: Path, file: str) -> bool:
    return queue_path.exists() and any(entry["file"] == file for entry in queue_store.load_queue(queue_path))


def queue_clip(name: str) -> dict:
    with QUEUE_LOCK:
        clip = next((clip for clip in load_clips() if clip["name"] == name), None)
        if clip is None:
            raise ValueError(f"no clip named '{name}'")
        file = (Path("clips") / clip["streamer"] / f"{name}.mp4").as_posix()
        if not Path(file).is_file():
            raise ValueError(f"'{name}' is not rendered yet: render it first")
        missing = [platform for platform, queue_path in (("youtube", YOUTUBE_QUEUE), ("facebook", FACEBOOK_QUEUE))
                   if not in_queue(queue_path, file)]
        if not missing:
            raise ValueError(f"'{name}' is already in both queues")
        presets = load_presets()
        slots = {}
        try:
            for platform in missing:
                slots[platform] = add_to_queue(platform, clip, file, presets)
        except OSError as error:
            raise ValueError(f"could not write the queue: {error}")
        logger.info(f"'{name}': queued {slots}")
        return slots


def add_to_queue(platform: str, clip: dict, file: str, presets: dict) -> str:
    # a platform already holding the clip is left alone, so a half-queued clip can be finished by pressing again
    queue_path = YOUTUBE_QUEUE if platform == "youtube" else FACEBOOK_QUEUE
    if not queue_path.exists():
        queue_path.write_text("[]", encoding="utf8")
    slot = next_slot(queue_path)
    text = build_text(clip, platform, presets)
    if platform == "youtube":
        problems = queue_store.add_entry({"file": file, "title": text["title"], "description": text["description"],
                                          "publish_at": slot}, queue_path)
    else:
        problems = facebook_queue.add_entry({"file": file, "caption": text["caption"], "publish_at": slot}, queue_path)
    if problems:
        raise ValueError(f"{platform}: " + "; ".join(problems))
    return slot
