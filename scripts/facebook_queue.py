import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from clip_store import CLIPS_DIR
from facebook_schedule import DEFAULT_QUEUE, check_entry
from youtube_schedule import save_queue

DHAKA = timezone(timedelta(hours=6))
# Meta's limit of API-published posts per 24 hours; whether Business Suite reels count towards it is unknown
DAILY_LIMIT = 30
# only these keys are kept: a request must not be able to set reel_id or status, which mark an entry as uploaded
ENTRY_KEYS = ("file", "caption", "publish_at")


def load_queue(path: Path = DEFAULT_QUEUE):
    return json.loads(path.read_text(encoding="utf8")) if path.exists() else []


def add_entry(entry: dict, path: Path = DEFAULT_QUEUE, now: datetime | None = None, clips_dir: Path = CLIPS_DIR):
    entry = {key: entry[key] for key in ENTRY_KEYS if key in entry}
    problems = check_entry(entry, now, clips_dir)
    if not problems:
        queue = load_queue(path)
        queue.append(entry)
        save_queue(path, queue)
    return problems


def per_day_warning(queue: list, day: date):
    return sum(datetime.fromisoformat(entry["publish_at"]).astimezone(DHAKA).date() == day for entry in queue) > DAILY_LIMIT
