import json
import logging
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import facebook_schedule
from facebook_schedule import check_entry, run, token_status

DHAKA = timezone(timedelta(hours=6))
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=DHAKA)
TOKEN = "SECRETTOKEN123"
CREDENTIALS = {"page_id": "555", "page_access_token": TOKEN}


class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.status_code = 200

    def json(self):
        return self.body


class FakeSession:
    """Records every call; `handler(method, url, kwargs)` returns the JSON answer or raises."""

    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler or self.default_handler

    @staticmethod
    def default_handler(method, url, kwargs):
        if "debug_token" in url:
            return {"data": {"type": "PAGE", "is_valid": True, "expires_at": 0,
                             "data_access_expires_at": int((NOW + timedelta(days=80)).timestamp()), "scopes": ["a"]}}
        if "rupload" in url:
            return {"success": True}
        if url.endswith("/video_reels") and kwargs["data"]["upload_phase"] == "start":
            return {"video_id": "V1", "upload_url": "https://rupload.facebook.com/video-upload/v26.0/V1"}
        if url.endswith("/video_reels"):
            return {"success": True}
        return {"name": "Viral Pulse HQ", "id": "555"}

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse(self.handler(method, url, kwargs))

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)


class Records(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def capture_logs():
    records = Records()
    logger = logging.getLogger("facebook_schedule")
    logger.setLevel(logging.INFO)
    logger.addHandler(records)
    return records, logger


def make_setup(entries_changes=None, count=1):
    folder = Path(tempfile.mkdtemp())
    clips_dir = folder / "clips"
    (clips_dir / "tomo").mkdir(parents=True)
    entries = []
    for index in range(count):
        clip = clips_dir / "tomo" / f"clip{index}.mp4"
        clip.write_bytes(b"video")
        entry = {"file": str(clip), "caption": f"Caption {index} #gta6",
                 "publish_at": (NOW + timedelta(days=2, hours=index)).isoformat(timespec="seconds")}
        entry.update((entries_changes or {}))
        entries.append(entry)
    queue_path = folder / "facebook_queue.json"
    queue_path.write_text(json.dumps(entries), encoding="utf8")
    return queue_path, clips_dir


def entry_for(clips_dir: Path, **changes):
    clip = clips_dir / "tomo" / "one.mp4"
    clip.write_bytes(b"video")
    entry = {"file": str(clip), "caption": "A caption", "publish_at": (NOW + timedelta(days=2)).isoformat(timespec="seconds")}
    entry.update(changes)
    return entry


def test_good_entry_passes():
    _, clips_dir = make_setup()
    assert check_entry(entry_for(clips_dir), NOW, clips_dir) == []


def test_missing_file_fails():
    _, clips_dir = make_setup()
    assert any("file" in problem for problem in check_entry(entry_for(clips_dir, file=str(clips_dir / "tomo" / "no.mp4")), NOW, clips_dir))


def test_non_mp4_fails():
    _, clips_dir = make_setup()
    other = clips_dir / "tomo" / "note.txt"
    other.write_text("x")
    assert any("file" in problem for problem in check_entry(entry_for(clips_dir, file=str(other)), NOW, clips_dir))


def test_no_timezone_fails():
    _, clips_dir = make_setup()
    assert any("publish_at" in problem for problem in check_entry(entry_for(clips_dir, publish_at="2026-11-26T10:00:00"), NOW, clips_dir))


def test_under_10_minutes_fails():
    _, clips_dir = make_setup()
    soon = (NOW + timedelta(minutes=9)).isoformat(timespec="seconds")
    assert any("publish_at" in problem for problem in check_entry(entry_for(clips_dir, publish_at=soon), NOW, clips_dir))
    fine = (NOW + timedelta(minutes=11)).isoformat(timespec="seconds")
    assert check_entry(entry_for(clips_dir, publish_at=fine), NOW, clips_dir) == []


def test_over_29_days_fails():
    _, clips_dir = make_setup()
    far = (NOW + timedelta(days=29, minutes=5)).isoformat(timespec="seconds")
    assert any("publish_at" in problem for problem in check_entry(entry_for(clips_dir, publish_at=far), NOW, clips_dir))


def test_file_outside_clips_fails():
    _, clips_dir = make_setup()
    outside = Path(tempfile.mkdtemp()) / "x.mp4"
    outside.write_bytes(b"v")
    assert any("file" in problem for problem in check_entry(entry_for(clips_dir, file=str(outside)), NOW, clips_dir))


def test_upload_sets_reel_id_before_finish():
    queue_path, clips_dir = make_setup()

    def handler(method, url, kwargs):
        if url.endswith("/video_reels") and kwargs["data"].get("upload_phase") != "start":
            raise RuntimeError("network died at the finish call")
        return FakeSession.default_handler(method, url, kwargs)

    records, logger = capture_logs()
    run(queue_path, dry_run=False, session=FakeSession(handler), now=NOW, clips_dir=clips_dir, credentials=CREDENTIALS)
    saved = json.loads(queue_path.read_text(encoding="utf8"))[0]
    logger.removeHandler(records)
    assert saved.get("reel_id") == "V1" and "status" not in saved, saved


def test_good_upload_marks_scheduled_and_sends_the_right_calls():
    queue_path, clips_dir = make_setup()
    session = FakeSession()
    run(queue_path, dry_run=False, session=session, now=NOW, clips_dir=clips_dir, credentials=CREDENTIALS)
    saved = json.loads(queue_path.read_text(encoding="utf8"))[0]
    assert saved["reel_id"] == "V1" and saved["status"] == "scheduled"
    finish = [kwargs for method, url, kwargs in session.calls if url.endswith("/video_reels") and kwargs["data"]["upload_phase"] != "start"][0]
    assert finish["data"]["video_state"] == "SCHEDULED" and finish["data"]["description"] == "Caption 0 #gta6"
    assert finish["data"]["video_id"] == "V1" and int(finish["data"]["scheduled_publish_time"]) == int((NOW + timedelta(days=2)).timestamp())


def test_entry_with_reel_id_is_never_uploaded_again():
    queue_path, clips_dir = make_setup({"reel_id": "OLD1"})
    session = FakeSession()
    records, logger = capture_logs()
    run(queue_path, dry_run=False, session=session, now=NOW, clips_dir=clips_dir, credentials=CREDENTIALS)
    logger.removeHandler(records)
    assert not [call for call in session.calls if call[1].endswith("/video_reels") or "rupload" in call[1]]
    assert any("needs a check" in line for line in records.lines)
    assert json.loads(queue_path.read_text(encoding="utf8"))[0]["reel_id"] == "OLD1"


def test_dry_run_makes_no_network_call():
    queue_path, clips_dir = make_setup()
    session = FakeSession()
    run(queue_path, dry_run=True, session=session, now=NOW, clips_dir=clips_dir, credentials=CREDENTIALS)
    assert session.calls == []
    assert "reel_id" not in json.loads(queue_path.read_text(encoding="utf8"))[0]


def test_auth_error_stops_the_run_without_marking_failures():
    queue_path, clips_dir = make_setup(count=3)
    session = FakeSession(lambda method, url, kwargs: {"error": {"code": 190, "message": "token " + TOKEN + " expired"}})
    records, logger = capture_logs()
    run(queue_path, dry_run=False, session=session, now=NOW, clips_dir=clips_dir, credentials=CREDENTIALS)
    logger.removeHandler(records)
    assert len(session.calls) == 1
    assert any("token expired, redo the token steps" in line for line in records.lines)
    assert not any("failed [" in line and "failed [0]" not in line for line in records.lines)
    assert all("reel_id" not in entry for entry in json.loads(queue_path.read_text(encoding="utf8")))


def test_token_never_logged():
    queue_path, clips_dir = make_setup(count=2)

    def handler(method, url, kwargs):
        if url.endswith("/video_reels") and kwargs["data"].get("upload_phase") == "finish":
            raise RuntimeError("boom with " + TOKEN)
        return FakeSession.default_handler(method, url, kwargs)

    records, logger = capture_logs()
    run(queue_path, dry_run=False, session=FakeSession(handler), now=NOW, clips_dir=clips_dir, credentials=CREDENTIALS)
    logger.removeHandler(records)
    assert records.lines and not any(TOKEN in line for line in records.lines)


def test_token_status_warns_when_data_access_ends_soon():
    def handler(method, url, kwargs):
        return {"data": {"type": "PAGE", "is_valid": True, "expires_at": 0,
                         "data_access_expires_at": int((NOW + timedelta(days=5)).timestamp()), "scopes": ["pages_manage_posts"]}}

    records, logger = capture_logs()
    status = token_status(FakeSession(handler), CREDENTIALS, NOW)
    logger.removeHandler(records)
    assert status["type"] == "PAGE" and status["expires_at"] == "never"
    assert TOKEN not in json.dumps(status)
    assert len([line for line in records.lines if "data access" in line]) == 1


def test_token_status_quiet_when_far_away_or_never():
    records, logger = capture_logs()
    token_status(FakeSession(), CREDENTIALS, NOW)
    logger.removeHandler(records)
    assert not any("data access" in line for line in records.lines)


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
            print("ok", name)
