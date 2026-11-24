import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import instagram_queue
import instagram_schedule

DHAKA = timezone(timedelta(hours=6))
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=DHAKA)


def make_setup():
    folder = Path(tempfile.mkdtemp())
    clips_dir = folder / "clips"
    (clips_dir / "speed").mkdir(parents=True)
    clip = clips_dir / "speed" / "one.mp4"
    clip.write_bytes(b"video")
    queue_path = folder / "instagram_queue.json"
    queue_path.write_text("[]", encoding="utf8")
    return queue_path, clips_dir, clip


def make_entry(clip: Path, **changes):
    entry = {"file": str(clip), "caption": "A caption #gta6", "publish_at": (NOW + timedelta(days=1)).isoformat(timespec="seconds")}
    entry.update(changes)
    return entry


def fake_copy(source, target):
    Path(target).write_bytes(b"light")


def add(entry, queue_path, clips_dir):
    return instagram_queue.add_entry(entry, queue_path, NOW, clips_dir, make_copy=fake_copy)


def test_an_entry_is_queued_with_the_lighter_ig_copy_and_only_known_keys():
    queue_path, clips_dir, clip = make_setup()
    assert add(make_entry(clip, evil="x", status="published"), queue_path, clips_dir) == []
    queue = json.loads(queue_path.read_text(encoding="utf8"))
    assert len(queue) == 1 and set(queue[0]) == {"file", "caption", "publish_at"}
    assert Path(queue[0]["file"]).name == "ig_one.mp4" and Path(queue[0]["file"]).is_file()


def test_a_clip_that_is_already_a_light_copy_is_not_copied_again():
    queue_path, clips_dir, clip = make_setup()
    light = clip.with_name("ig_two.mp4")
    light.write_bytes(b"light")
    assert instagram_queue.add_entry(make_entry(light), queue_path, NOW, clips_dir, make_copy=None) == []
    assert Path(json.loads(queue_path.read_text(encoding="utf8"))[0]["file"]).name == "ig_two.mp4"


def test_a_past_time_a_missing_file_or_a_duplicate_is_refused_and_nothing_is_written():
    queue_path, clips_dir, clip = make_setup()
    past = make_entry(clip, publish_at=(NOW - timedelta(hours=1)).isoformat(timespec="seconds"))
    assert any("publish_at" in problem for problem in add(past, queue_path, clips_dir))
    assert any("file" in problem for problem in add(make_entry(clip.with_name("none.mp4")), queue_path, clips_dir))
    assert add(make_entry(clip), queue_path, clips_dir) == []
    assert any("already" in problem for problem in add(make_entry(clip), queue_path, clips_dir))
    assert len(json.loads(queue_path.read_text(encoding="utf8"))) == 1


def test_the_entry_is_refused_while_the_watcher_holds_the_queue():
    queue_path, clips_dir, clip = make_setup()
    with instagram_schedule.queue_lock(queue_path) as got:
        assert got
        assert any("watcher" in problem for problem in add(make_entry(clip), queue_path, clips_dir))
    assert json.loads(queue_path.read_text(encoding="utf8")) == []


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
            print("ok", name)
