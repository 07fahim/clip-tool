import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from youtube_schedule import DEFAULT_QUEUE, check_entry, save_queue

DHAKA = timezone(timedelta(hours=6))
# the hours the channel already posts at (Dhaka time)
SLOT_TIMES = [(7, 30), (9, 0), (18, 0), (20, 0), (22, 0)]
# days ahead that suggest_slots looks through before giving up
SLOT_SEARCH_DAYS = 14
# YouTube's upload limit per day, from the notes in memory, not re-verified
DAILY_UPLOAD_LIMIT = 10
ENTRY_KEYS = ("file", "title", "description", "publish_at")


def load_queue(path: Path = DEFAULT_QUEUE):
    return json.loads(path.read_text(encoding="utf8"))


def add_entry(entry: dict, path: Path = DEFAULT_QUEUE):
    # only these keys are kept: a request must not be able to set video_id, which marks an entry as uploaded
    entry = {key: entry[key] for key in ENTRY_KEYS if key in entry}
    entry.setdefault("description", "")
    problems = check_entry(entry)
    if not isinstance(entry["description"], str):
        problems.append("description: must be text")
    if not problems:
        queue = load_queue(path)
        queue.append(entry)
        save_queue(path, queue)
    return problems


def update_entry(index: int, changes: dict, path: Path = DEFAULT_QUEUE):
    queue = load_queue(path)
    if queue[index].get("video_id"):
        return [f"'{queue[index]['title']}' is already uploaded and cannot be edited"]
    updated = dict(queue[index], **changes)
    problems = check_entry(updated)
    if not problems:
        queue[index] = updated
        save_queue(path, queue)
    return problems


def suggest_slots(queue: list, count: int, now: datetime | None = None):
    now = now or datetime.now(DHAKA)
    taken = {datetime.fromisoformat(entry["publish_at"]) for entry in queue}
    slots = []
    for day_offset in range(SLOT_SEARCH_DAYS):
        day = (now + timedelta(days=day_offset)).date()
        for hour, minute in SLOT_TIMES:
            slot = datetime(day.year, day.month, day.day, hour, minute, tzinfo=DHAKA)
            if slot > now and slot not in taken:
                slots.append(slot.isoformat(timespec="seconds"))
                if len(slots) == count:
                    return slots
    return slots


def uploads_on_day(queue: list, day: date):
    return sum(datetime.fromisoformat(entry["publish_at"]).astimezone(DHAKA).date() == day for entry in queue)
