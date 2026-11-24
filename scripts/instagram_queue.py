import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from clip_store import CLIPS_DIR
from instagram_schedule import DEFAULT_QUEUE, check_entry, queue_lock
from youtube_schedule import save_queue

DHAKA = timezone(timedelta(hours=6))
# only these keys are kept: a request must not be able to set status or media_id, which mark an entry as published
ENTRY_KEYS = ("file", "caption", "publish_at")
LIGHT_PREFIX = "ig_"
COPY_TIMEOUT_SECONDS = 900


def load_queue(path: Path = DEFAULT_QUEUE):
    return json.loads(path.read_text(encoding="utf8")) if path.exists() else []


def make_light_copy(source: Path, target: Path):
    # 2026-11-24: the 60 fps originals (19 to 54 MB) hung at Meta; 30 fps copies of 16 to 23 MB went through
    temporary = target.with_name(target.stem + ".tmp.mp4")
    try:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(source), "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(temporary)],
                       check=True, timeout=COPY_TIMEOUT_SECONDS)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def add_entry(entry: dict, path: Path = DEFAULT_QUEUE, now: datetime | None = None, clips_dir: Path = CLIPS_DIR, make_copy=make_light_copy):
    now = now or datetime.now(timezone.utc)
    entry = {key: entry[key] for key in ENTRY_KEYS if key in entry}
    problems = check_entry(entry, clips_dir)
    if not problems:
        try:
            if datetime.fromisoformat(entry["publish_at"]) <= now:
                problems.append("publish_at must be in the future")
        except ValueError:
            pass
    if problems:
        return problems
    source = Path(entry["file"])
    light = source if source.name.startswith(LIGHT_PREFIX) else source.with_name(LIGHT_PREFIX + source.name)
    if not light.is_file():
        make_copy(source, light)
    entry["file"] = light.as_posix()
    with queue_lock(path) as got:
        if not got:
            return ["the Instagram watcher is posting right now; try again in a minute"]
        queue = load_queue(path)
        if any(stored.get("file") == entry["file"] and stored.get("publish_at") == entry["publish_at"] for stored in queue):
            return ["this clip is already queued for that time"]
        queue.append(entry)
        save_queue(path, queue)
    return []
