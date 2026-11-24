import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from youtube_schedule import check_entry

DHAKA = timezone(timedelta(hours=6))


def make_entry(**changes):
    future = (datetime.now(DHAKA) + timedelta(hours=3)).isoformat(timespec="seconds")
    entry = {"file": __file__, "title": "A title", "description": "A description #shorts", "publish_at": future}
    entry.update(changes)
    return entry


def test_good_entry_passes():
    assert check_entry(make_entry()) == []


def test_missing_file_fails():
    assert any("file" in problem for problem in check_entry(make_entry(file="no_such_clip.mp4")))


def test_long_title_fails():
    assert any("title" in problem for problem in check_entry(make_entry(title="x" * 101)))


def test_time_without_timezone_fails():
    assert any("publish_at" in problem for problem in check_entry(make_entry(publish_at="2030-01-01T10:00:00")))


def test_past_time_fails():
    assert any("publish_at" in problem for problem in check_entry(make_entry(publish_at="2020-01-01T10:00:00+06:00")))


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
            print("ok", name)
