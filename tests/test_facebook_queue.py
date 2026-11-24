import json
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from facebook_queue import add_entry, load_queue, per_day_warning

DHAKA = timezone(timedelta(hours=6))
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=DHAKA)


def make_setup():
    folder = Path(tempfile.mkdtemp())
    clips_dir = folder / "clips"
    (clips_dir / "tomo").mkdir(parents=True)
    clip = clips_dir / "tomo" / "one.mp4"
    clip.write_bytes(b"video")
    queue_path = folder / "facebook_queue.json"
    queue_path.write_text("[]", encoding="utf8")
    return queue_path, clips_dir, clip


def make_entry(clip: Path, **changes):
    entry = {"file": str(clip), "caption": "A caption #gta6", "publish_at": (NOW + timedelta(days=2)).isoformat(timespec="seconds")}
    entry.update(changes)
    return entry


def test_add_good_entry_saves_it():
    queue_path, clips_dir, clip = make_setup()
    assert add_entry(make_entry(clip), queue_path, NOW, clips_dir) == []
    assert len(load_queue(queue_path)) == 1


def test_add_keeps_only_known_keys():
    queue_path, clips_dir, clip = make_setup()
    assert add_entry(make_entry(clip, evil="x", title="not used"), queue_path, NOW, clips_dir) == []
    assert sorted(load_queue(queue_path)[0]) == ["caption", "file", "publish_at"]


def test_add_cannot_set_reel_id_or_status():
    queue_path, clips_dir, clip = make_setup()
    add_entry(make_entry(clip, reel_id="forged", status="scheduled"), queue_path, NOW, clips_dir)
    saved = load_queue(queue_path)[0]
    assert "reel_id" not in saved and "status" not in saved


def test_add_refuses_bad_time():
    queue_path, clips_dir, clip = make_setup()
    for bad in ("2026-11-26T10:00:00", (NOW + timedelta(minutes=5)).isoformat(), (NOW + timedelta(days=40)).isoformat(), "x"):
        assert any("publish_at" in problem for problem in add_entry(make_entry(clip, publish_at=bad), queue_path, NOW, clips_dir)), bad
    assert load_queue(queue_path) == []


def test_add_refuses_non_text_caption():
    queue_path, clips_dir, clip = make_setup()
    assert any("caption" in problem for problem in add_entry(make_entry(clip, caption=5), queue_path, NOW, clips_dir))


def test_crash_mid_write_keeps_old_queue():
    queue_path, clips_dir, clip = make_setup()
    add_entry(make_entry(clip, caption="Old"), queue_path, NOW, clips_dir)
    real_write = Path.write_text

    def half_write(self, text, *args, **kwargs):
        real_write(self, text[: len(text) // 2], *args, **kwargs)
        raise OSError("disk full")

    Path.write_text = half_write
    try:
        try:
            add_entry(make_entry(clip, caption="New"), queue_path, NOW, clips_dir)
        except OSError:
            pass
    finally:
        Path.write_text = real_write
    assert [entry["caption"] for entry in load_queue(queue_path)] == ["Old"]


def test_per_day_warning_above_30():
    day = date(2026, 11, 26)
    entries = [{"publish_at": "2026-11-26T10:00:00+06:00"}] * 30
    assert per_day_warning(entries, day) is False
    assert per_day_warning(entries + [{"publish_at": "2026-11-26T11:00:00+06:00"}], day) is True
    assert per_day_warning(entries + [{"publish_at": "2026-11-27T01:00:00+06:00"}], day) is False


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
            print("ok", name)
