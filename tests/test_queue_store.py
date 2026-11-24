import json
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from queue_store import add_entry, load_queue, suggest_slots, update_entry, uploads_on_day

DHAKA = timezone(timedelta(hours=6))


def make_entry(**changes):
    entry = {"file": __file__, "title": "A title", "description": "A description #shorts",
             "publish_at": (datetime.now(DHAKA) + timedelta(days=2)).isoformat(timespec="seconds")}
    entry.update(changes)
    return entry


def temp_queue(entries: list):
    path = Path(tempfile.mkdtemp()) / "queue.json"
    path.write_text(json.dumps(entries), encoding="utf8")
    return path


def test_add_good_entry_saves_it():
    path = temp_queue([])
    assert add_entry(make_entry(), path) == []
    assert len(load_queue(path)) == 1


def test_add_cannot_set_video_id_or_other_keys():
    path = temp_queue([])
    assert add_entry(make_entry(video_id="forged", extra="x"), path) == []
    saved = load_queue(path)[0]
    assert "video_id" not in saved and "extra" not in saved


def test_add_rejects_non_text_description():
    path = temp_queue([])
    assert any("description" in problem for problem in add_entry(make_entry(description=5), path))


def test_add_rejects_no_timezone():
    path = temp_queue([])
    assert any("publish_at" in problem for problem in add_entry(make_entry(publish_at="2030-01-01T10:00:00"), path))
    assert load_queue(path) == []


def test_add_rejects_past_time():
    path = temp_queue([])
    assert any("publish_at" in problem for problem in add_entry(make_entry(publish_at="2020-01-01T10:00:00+06:00"), path))
    assert load_queue(path) == []


def test_entry_with_video_id_cannot_be_edited():
    path = temp_queue([make_entry(video_id="abc123")])
    assert any("already uploaded" in problem for problem in update_entry(0, {"title": "New"}, path))
    assert load_queue(path)[0]["title"] == "A title"


def test_entry_without_video_id_can_be_edited():
    path = temp_queue([make_entry()])
    assert update_entry(0, {"title": "New"}, path) == []
    assert load_queue(path)[0]["title"] == "New"


def test_suggest_slots_skips_taken_and_past():
    now = datetime(2026, 11, 23, 8, 0, tzinfo=DHAKA)
    taken = [make_entry(publish_at="2026-11-23T09:00:00+06:00")]
    slots = suggest_slots(taken, 3, now)
    assert slots == ["2026-11-23T18:00:00+06:00", "2026-11-23T20:00:00+06:00", "2026-11-23T22:00:00+06:00"]


def test_uploads_on_day_counts_per_dhaka_day():
    queue = [make_entry(publish_at="2026-11-23T07:30:00+06:00"), make_entry(publish_at="2026-11-23T22:00:00+06:00"),
             make_entry(publish_at="2026-11-24T00:30:00+06:00")]
    assert uploads_on_day(queue, date(2026, 11, 23)) == 2


def test_crash_mid_write_keeps_the_old_queue():
    import queue_store

    path = temp_queue([make_entry(title="Old")])
    real_write = Path.write_text

    def half_write(self, text, *args, **kwargs):
        real_write(self, text[: len(text) // 2], *args, **kwargs)
        raise OSError("disk full")

    Path.write_text = half_write
    try:
        try:
            queue_store.add_entry(make_entry(title="New"), path)
        except OSError:
            pass
    finally:
        Path.write_text = real_write
    assert load_queue(path)[0]["title"] == "Old" and len(load_queue(path)) == 1


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
            print("ok", name)
