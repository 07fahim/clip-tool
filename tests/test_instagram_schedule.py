import errno
import json
import logging
import msvcrt
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import instagram_schedule
from instagram_schedule import check_entry, load_credentials, run

DHAKA = timezone(timedelta(hours=6))
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=DHAKA)
TOKEN = "SECRETTOKEN123"
CREDENTIALS = {"ig_user_id": "999", "access_token": TOKEN}


class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.status_code = 200

    def json(self):
        return self.body


class FakeSession:
    # records every call; handler(method, url, kwargs) returns the JSON answer or raises
    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler or self.default_handler

    @staticmethod
    def default_handler(method, url, kwargs):
        if "rupload" in url:
            return {"success": True}
        if url.endswith("/999/media"):
            return {"id": "C1", "uri": "https://rupload.facebook.com/ig-api-upload/v26.0/C1"}
        if url.endswith("/999/media_publish"):
            return {"id": "M1"}
        if url.endswith("/C1"):
            return {"id": "C1", "status_code": "FINISHED"}
        return {"id": "999", "username": "viral_pulsehq1", "media_count": 0}

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
    logger = logging.getLogger("instagram_schedule")
    logger.setLevel(logging.INFO)
    logger.addHandler(records)
    return records, logger


def make_setup(changes=None, hours_from_now=-1, count=1):
    folder = Path(tempfile.mkdtemp())
    clips_dir = folder / "clips"
    (clips_dir / "speed").mkdir(parents=True)
    entries = []
    for index in range(count):
        clip = clips_dir / "speed" / f"clip{index}.mp4"
        clip.write_bytes(b"video")
        entry = {
            "file": str(clip),
            "caption": f"Caption {index} #gta6",
            "publish_at": (NOW + timedelta(hours=hours_from_now)).isoformat(timespec="seconds"),
        }
        entry.update(changes or {})
        entries.append(entry)
    queue_path = folder / "instagram_queue.json"
    queue_path.write_text(json.dumps(entries), encoding="utf8")
    return queue_path, clips_dir


def run_pass(queue_path, clips_dir, session, dry_run=False, now=NOW):
    run(
        queue_path,
        dry_run=dry_run,
        session=session,
        now=now,
        clips_dir=clips_dir,
        credentials=CREDENTIALS,
        pause=lambda seconds: None,
    )
    return json.loads(queue_path.read_text(encoding="utf8"))


def media_calls(session):
    return [call for call in session.calls if call[1].endswith("/999/media")]


def test_good_entry_passes():
    queue_path, clips_dir = make_setup()
    entry = json.loads(queue_path.read_text(encoding="utf8"))[0]
    assert check_entry(entry, clips_dir) == []


def test_bad_entries_fail():
    queue_path, clips_dir = make_setup()
    entry = json.loads(queue_path.read_text(encoding="utf8"))[0]
    assert any("file" in problem for problem in check_entry(dict(entry, file=str(clips_dir / "no.mp4")), clips_dir))
    outside = Path(tempfile.mkdtemp()) / "x.mp4"
    outside.write_bytes(b"v")
    assert any("file" in problem for problem in check_entry(dict(entry, file=str(outside)), clips_dir))
    assert any(
        "publish_at" in problem for problem in check_entry(dict(entry, publish_at="2026-11-24T10:00:00"), clips_dir)
    )
    assert any("caption" in problem for problem in check_entry(dict(entry, caption=5), clips_dir))


def test_due_entry_is_published_with_the_right_calls():
    queue_path, clips_dir = make_setup()
    session = FakeSession()
    saved = run_pass(queue_path, clips_dir, session)[0]
    assert saved["status"] == "published" and saved["container_id"] == "C1" and saved["media_id"] == "M1", saved
    create = media_calls(session)[0][2]["data"]
    assert (
        create["media_type"] == "REELS"
        and create["upload_type"] == "resumable"
        and create["caption"] == "Caption 0 #gta6"
    )
    order = [call[1].rsplit("/", 1)[-1] for call in session.calls]
    assert order.index("media") < order.index("C1") < order.index("media_publish")
    upload = [call for call in session.calls if "rupload" in call[1]][0]
    assert upload[2]["headers"]["offset"] == "0" and upload[2]["headers"]["file_size"] == str(len(b"video"))
    publish = [call for call in session.calls if call[1].endswith("/media_publish")][0]
    assert publish[2]["data"]["creation_id"] == "C1"


def test_future_entry_is_left_alone():
    queue_path, clips_dir = make_setup(hours_from_now=3)
    session = FakeSession()
    saved = run_pass(queue_path, clips_dir, session)[0]
    assert session.calls == [] and "status" not in saved and "container_id" not in saved


def test_container_id_saved_before_the_upload_bytes_go():
    queue_path, clips_dir = make_setup()

    def handler(method, url, kwargs):
        if "rupload" in url:
            raise RuntimeError("network died during the upload")
        return FakeSession.default_handler(method, url, kwargs)

    saved = run_pass(queue_path, clips_dir, FakeSession(handler))[0]
    assert saved.get("container_id") == "C1" and "status" not in saved, saved


def test_entry_with_container_id_is_never_created_again():
    queue_path, clips_dir = make_setup({"container_id": "C1"})
    session = FakeSession()
    saved = run_pass(queue_path, clips_dir, session)[0]
    assert not media_calls(session) and not [call for call in session.calls if "rupload" in call[1]]
    assert saved["status"] == "published" and saved["media_id"] == "M1"


def test_resumed_entry_that_is_not_finished_is_not_published():
    queue_path, clips_dir = make_setup({"container_id": "C1"})
    session = FakeSession(
        lambda method, url, kwargs: {"id": "C1", "status_code": "ERROR"}
        if url.endswith("/C1") and "rupload" not in url
        else FakeSession.default_handler(method, url, kwargs)
    )
    records, logger = capture_logs()
    saved = run_pass(queue_path, clips_dir, session)[0]
    logger.removeHandler(records)
    assert saved["status"] == "needs_check" and not [
        call for call in session.calls if call[1].endswith("/media_publish")
    ]
    assert any("needs a check" in line for line in records.lines), records.lines


def test_published_entry_is_skipped():
    queue_path, clips_dir = make_setup({"status": "published", "container_id": "C0", "media_id": "M0"})
    session = FakeSession()
    saved = run_pass(queue_path, clips_dir, session)[0]
    assert session.calls == [] and saved["media_id"] == "M0"


def test_entry_that_is_too_late_is_marked_missed_and_not_posted():
    queue_path, clips_dir = make_setup(hours_from_now=-(instagram_schedule.MAX_LATE_HOURS + 1))
    session = FakeSession()
    records, logger = capture_logs()
    saved = run_pass(queue_path, clips_dir, session)[0]
    logger.removeHandler(records)
    assert session.calls == [] and saved["status"] == "missed"
    assert any("missed" in line for line in records.lines)


def test_waits_while_the_video_is_processing():
    queue_path, clips_dir = make_setup()
    answers = iter(["IN_PROGRESS", "IN_PROGRESS", "FINISHED"])
    session = FakeSession(
        lambda method, url, kwargs: {"id": "C1", "status_code": next(answers)}
        if url.endswith("/C1") and "rupload" not in url
        else FakeSession.default_handler(method, url, kwargs)
    )
    assert run_pass(queue_path, clips_dir, session)[0]["status"] == "published"


def test_gives_up_when_the_video_never_finishes():
    queue_path, clips_dir = make_setup()
    session = FakeSession(
        lambda method, url, kwargs: {"id": "C1", "status_code": "IN_PROGRESS"}
        if url.endswith("/C1") and "rupload" not in url
        else FakeSession.default_handler(method, url, kwargs)
    )
    saved = run_pass(queue_path, clips_dir, session)[0]
    status_calls = [call for call in session.calls if call[0] == "GET" and call[1].endswith("/C1")]
    assert len(status_calls) == instagram_schedule.MAX_POLLS and "status" not in saved
    assert not [call for call in session.calls if call[1].endswith("/media_publish")]


def test_dry_run_makes_no_network_call():
    queue_path, clips_dir = make_setup()
    session = FakeSession()
    saved = run_pass(queue_path, clips_dir, session, dry_run=True)[0]
    assert session.calls == [] and "container_id" not in saved


def test_auth_error_stops_the_run():
    queue_path, clips_dir = make_setup(count=3)
    session = FakeSession(
        lambda method, url, kwargs: {"error": {"code": 190, "message": "token " + TOKEN + " expired"}}
    )
    records, logger = capture_logs()
    saved = run_pass(queue_path, clips_dir, session)
    logger.removeHandler(records)
    assert len(session.calls) == 1
    assert any("token expired" in line for line in records.lines)
    assert all("container_id" not in entry for entry in saved)


def test_token_never_logged_and_one_failure_does_not_stop_the_next():
    queue_path, clips_dir = make_setup(count=2)

    def handler(method, url, kwargs):
        if url.endswith("/999/media_publish") and not any(
            call[1].endswith("/media_publish") for call in session.calls[:-1]
        ):
            raise RuntimeError("boom with " + TOKEN)
        return FakeSession.default_handler(method, url, kwargs)

    session = FakeSession(handler)
    records, logger = capture_logs()
    saved = run_pass(queue_path, clips_dir, session)
    logger.removeHandler(records)
    assert records.lines and not any(TOKEN in line for line in records.lines)
    assert saved[0].get("status") != "published" and saved[1]["status"] == "published", saved


def test_credentials_placeholder_is_refused():
    path = Path(tempfile.mkdtemp()) / "instagram.json"
    path.write_text(json.dumps({"ig_user_id": "PASTE_ID_HERE", "access_token": "x"}), encoding="utf8")
    try:
        load_credentials(path)
    except SystemExit:
        return
    raise AssertionError("placeholder id was accepted")


def test_meta_already_shows_the_reel_as_published():
    queue_path, clips_dir = make_setup({"container_id": "C1", "uploaded": True})
    session = FakeSession(
        lambda method, url, kwargs: {"id": "C1", "status_code": "PUBLISHED"}
        if url.endswith("/C1") and "rupload" not in url
        else FakeSession.default_handler(method, url, kwargs)
    )
    records, logger = capture_logs()
    saved = run_pass(queue_path, clips_dir, session)[0]
    logger.removeHandler(records)
    assert saved["status"] == "published" and saved["media_id"] == "unknown", saved
    assert not [call for call in session.calls if call[1].endswith("/media_publish")]
    assert any("check the account" in line for line in records.lines), records.lines


def test_interrupted_upload_is_sent_again_on_the_next_pass():
    queue_path, clips_dir = make_setup()
    broken = {"on": True}

    def handler(method, url, kwargs):
        if "rupload" in url and broken["on"]:
            raise RuntimeError("network died during the upload")
        return FakeSession.default_handler(method, url, kwargs)

    session = FakeSession(handler)
    first = run_pass(queue_path, clips_dir, session)[0]
    assert first["container_id"] == "C1" and "uploaded" not in first and "status" not in first, first
    broken["on"] = False
    second = run_pass(
        queue_path, clips_dir, session, now=NOW + timedelta(minutes=instagram_schedule.RETRY_MINUTES + 1)
    )[0]
    assert second["status"] == "published", second
    assert len(media_calls(session)) == 1, "the container must not be created a second time"
    assert len([call for call in session.calls if "rupload" in call[1]]) == 2


def test_answer_without_upload_address_fails_the_entry_without_a_crash():
    queue_path, clips_dir = make_setup(count=2)
    session = FakeSession(
        lambda method, url, kwargs: {"id": "C1"}
        if url.endswith("/999/media")
        else FakeSession.default_handler(method, url, kwargs)
    )
    saved = run_pass(queue_path, clips_dir, session)
    assert saved[0].get("container_id") == "C1" and "status" not in saved[0]
    assert saved[1].get("container_id") == "C1", "the next entry must still be tried"


def test_repeated_failures_stop_the_retries():
    queue_path, clips_dir = make_setup()
    session = FakeSession(lambda method, url, kwargs: {"error": {"code": 100, "message": "caption too long"}})
    # one pass an hour: far enough apart that the waiting time between tries has always passed
    for index in range(instagram_schedule.MAX_ATTEMPTS):
        saved = run_pass(queue_path, clips_dir, session, now=NOW + timedelta(hours=index))[0]
    assert saved["status"] == "needs_check" and "caption too long" in saved["error"], saved
    calls_before = len(session.calls)
    run_pass(queue_path, clips_dir, session, now=NOW + timedelta(hours=9))
    assert len(session.calls) == calls_before, "a needs_check entry must not be tried again"


def test_failed_entry_waits_longer_before_each_next_try():
    queue_path, clips_dir = make_setup()
    session = FakeSession(lambda method, url, kwargs: {"error": {"code": 2, "message": "temporary Meta trouble"}})
    first = run_pass(queue_path, clips_dir, session)[0]
    wait = instagram_schedule.RETRY_MINUTES
    assert first["attempts"] == 1 and datetime.fromisoformat(first["retry_after"]) == NOW + timedelta(minutes=wait), (
        first
    )
    calls_after_first = len(session.calls)
    run_pass(queue_path, clips_dir, session, now=NOW + timedelta(minutes=wait - 1))
    assert len(session.calls) == calls_after_first, "tried again before the waiting time was over"
    second = run_pass(queue_path, clips_dir, session, now=NOW + timedelta(minutes=wait + 1))[0]
    assert len(session.calls) > calls_after_first and second["attempts"] == 2, second
    assert datetime.fromisoformat(second["retry_after"]) == NOW + timedelta(minutes=wait + 1 + 2 * wait), second


def test_published_entry_drops_its_retry_time():
    queue_path, clips_dir = make_setup(
        {"attempts": 1, "error": "temporary", "retry_after": (NOW - timedelta(minutes=5)).isoformat()}
    )
    saved = run_pass(queue_path, clips_dir, FakeSession())[0]
    assert saved["status"] == "published" and "retry_after" not in saved and "error" not in saved, saved


def test_queue_edited_during_a_pass_is_not_lost():
    queue_path, clips_dir = make_setup()
    extra = {
        "file": str(next(clips_dir.rglob("*.mp4"))),
        "caption": "Added by hand",
        "publish_at": "2030-01-01T10:00:00+06:00",
    }

    def handler(method, url, kwargs):
        if "rupload" in url:
            queue = json.loads(queue_path.read_text(encoding="utf8"))
            queue.append(extra)
            queue_path.write_text(json.dumps(queue), encoding="utf8")
        return FakeSession.default_handler(method, url, kwargs)

    saved = run_pass(queue_path, clips_dir, FakeSession(handler))
    assert [entry["caption"] for entry in saved] == ["Caption 0 #gta6", "Added by hand"], saved
    assert saved[0]["status"] == "published"


def test_save_failure_after_publishing_does_not_crash_the_run():
    queue_path, clips_dir = make_setup(count=2)
    real_save = instagram_schedule.save_queue

    def failing_save(path, queue):
        if any(entry.get("status") == "published" for entry in queue):
            raise OSError("disk full")
        real_save(path, queue)

    instagram_schedule.save_queue = failing_save
    records, logger = capture_logs()
    try:
        run_pass(queue_path, clips_dir, FakeSession())
    finally:
        instagram_schedule.save_queue = real_save
        logger.removeHandler(records)
    assert any("queue was not saved" in line for line in records.lines), records.lines


def test_auth_error_makes_run_return_false():
    queue_path, clips_dir = make_setup()
    session = FakeSession(lambda method, url, kwargs: {"error": {"code": 190, "message": "expired"}})
    result = run(
        queue_path,
        dry_run=False,
        session=session,
        now=NOW,
        clips_dir=clips_dir,
        credentials=CREDENTIALS,
        pause=lambda seconds: None,
    )
    assert result is False
    assert run(queue_path, dry_run=True, now=NOW, clips_dir=clips_dir) is True


def test_watch_survives_a_broken_queue_and_stops_on_an_auth_error():
    queue_path, clips_dir = make_setup()
    good_queue = queue_path.read_text(encoding="utf8")
    queue_path.write_text('[{"half written', encoding="utf8")
    session = FakeSession(lambda method, url, kwargs: {"error": {"code": 190, "message": "expired"}})
    pauses = []

    def pause(seconds):
        pauses.append(seconds)
        queue_path.write_text(good_queue, encoding="utf8")

    records, logger = capture_logs()
    instagram_schedule.watch(
        queue_path, False, pause=pause, session=session, now=NOW, clips_dir=clips_dir, credentials=CREDENTIALS
    )
    logger.removeHandler(records)
    assert any("queue file" in line for line in records.lines), records.lines
    assert pauses == [instagram_schedule.WATCH_SECONDS] and len(session.calls) == 1


def test_watch_survives_any_error_in_a_pass_and_logs_it():
    queue_path, clips_dir = make_setup()
    good_queue = queue_path.read_text(encoding="utf8")
    # a bare number is a wrong shape that no single entry can absorb: the whole pass fails and the watch must go on
    queue_path.write_text("5", encoding="utf8")
    session = FakeSession(lambda method, url, kwargs: {"error": {"code": 190, "message": "expired"}})

    def pause(seconds):
        queue_path.write_text(good_queue, encoding="utf8")

    records, logger = capture_logs()
    instagram_schedule.watch(
        queue_path, False, pause=pause, session=session, now=NOW, clips_dir=clips_dir, credentials=CREDENTIALS
    )
    logger.removeHandler(records)
    assert any("pass failed" in line for line in records.lines), records.lines
    assert len(session.calls) == 1


def test_upload_answer_without_success_is_not_counted_as_uploaded():
    queue_path, clips_dir = make_setup()
    # the shape of a rejected upload seen live: no "error" key, no "success"
    def handler(method, url, kwargs):
        if "rupload" in url:
            return {"debug_info": {"retriable": True, "message": "not received"}}
        # the container has not taken a video: Meta still shows it as processing nothing
        if url.endswith("/C1"):
            return {"id": "C1", "status_code": "IN_PROGRESS"}
        return FakeSession.default_handler(method, url, kwargs)

    session = FakeSession(handler)
    saved = run_pass(queue_path, clips_dir, session)[0]
    assert "uploaded" not in saved and saved.get("attempts") == 1 and "status" not in saved, saved
    assert not [call for call in session.calls if call[1].endswith("/media_publish")]


def test_a_bad_retry_time_does_not_stop_the_other_entries():
    for bad in ("garbage", "2026-11-24T12:30:00", 5):
        queue_path, clips_dir = make_setup(count=2)
        queue = json.loads(queue_path.read_text(encoding="utf8"))
        queue[0]["retry_after"] = bad
        queue_path.write_text(json.dumps(queue), encoding="utf8")
        records, logger = capture_logs()
        saved = run_pass(queue_path, clips_dir, FakeSession())
        logger.removeHandler(records)
        assert saved[1]["status"] == "published", (bad, saved)
        assert any("retry_after" in line for line in records.lines), (bad, records.lines)


def test_an_entry_that_is_not_an_object_does_not_stop_the_others():
    queue_path, clips_dir = make_setup()
    queue = json.loads(queue_path.read_text(encoding="utf8"))
    queue_path.write_text(json.dumps(["oops"] + queue), encoding="utf8")
    saved = run_pass(queue_path, clips_dir, FakeSession())
    assert saved[1]["status"] == "published", saved


def test_a_failure_while_recording_a_failure_does_not_stop_the_others():
    queue_path, clips_dir = make_setup(count=2)
    queue = json.loads(queue_path.read_text(encoding="utf8"))
    queue[0]["attempts"] = "two"
    queue_path.write_text(json.dumps(queue), encoding="utf8")
    created = []

    def handler(method, url, kwargs):
        if url.endswith("/999/media"):
            created.append(url)
            if len(created) == 1:
                return {"error": {"code": 100, "message": "first one is refused"}}
        return FakeSession.default_handler(method, url, kwargs)

    records, logger = capture_logs()
    saved = run_pass(queue_path, clips_dir, FakeSession(handler))
    logger.removeHandler(records)
    assert saved[1]["status"] == "published", saved
    assert any("unexpected" in line for line in records.lines), records.lines


def test_ctrl_c_passes_through_watch_and_the_lock_is_released():
    queue_path, clips_dir = make_setup()

    def handler(method, url, kwargs):
        raise KeyboardInterrupt()

    try:
        instagram_schedule.watch(
            queue_path,
            False,
            pause=lambda seconds: None,
            session=FakeSession(handler),
            now=NOW,
            clips_dir=clips_dir,
            credentials=CREDENTIALS,
        )
    except KeyboardInterrupt:
        with instagram_schedule.queue_lock(queue_path) as got_lock:
            assert got_lock, "Ctrl+C left the lock held"
        return
    raise AssertionError("Ctrl+C was swallowed")


def test_two_entries_with_the_same_file_and_time_publish_only_once():
    queue_path, clips_dir = make_setup()
    queue = json.loads(queue_path.read_text(encoding="utf8"))
    queue_path.write_text(json.dumps(queue + [dict(queue[0], caption="A different caption")]), encoding="utf8")
    session = FakeSession()
    records, logger = capture_logs()
    saved = run_pass(queue_path, clips_dir, session)
    logger.removeHandler(records)
    assert saved[0]["status"] == "published" and "status" not in saved[1], saved
    assert len(media_calls(session)) == 1
    assert any("duplicate" in line for line in records.lines), records.lines


def test_entry_with_an_unhashable_value_does_not_stop_the_others():
    for field in ("file", "publish_at"):
        queue_path, clips_dir = make_setup(count=2)
        queue = json.loads(queue_path.read_text(encoding="utf8"))
        queue[0][field] = ["x"]
        queue_path.write_text(json.dumps(queue), encoding="utf8")
        saved = run_pass(queue_path, clips_dir, FakeSession())
        assert saved[1]["status"] == "published", (field, saved)


def test_the_final_line_counts_what_happened():
    queue_path, clips_dir = make_setup()
    queue = json.loads(queue_path.read_text(encoding="utf8"))
    queue_path.write_text(json.dumps(queue + [dict(queue[0])]), encoding="utf8")
    records, logger = capture_logs()
    run_pass(queue_path, clips_dir, FakeSession())
    logger.removeHandler(records)
    assert "published [1], waiting [0], skipped [0], failed [1]" in records.lines[-1], records.lines[-1]


@contextmanager
def held_lock(queue_path):
    # another process holding the lock: a second open handle with the same byte locked
    with instagram_schedule.lock_path(queue_path).open("a+b") as handle:
        handle.write(b"x")
        handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def test_retry_time_counts_from_the_moment_of_the_failure():
    queue_path, clips_dir = make_setup()
    ticks = iter(range(100))

    def clock():
        return NOW + timedelta(minutes=7 * next(ticks))

    session = FakeSession(lambda method, url, kwargs: {"error": {"code": 2, "message": "temporary Meta trouble"}})
    run(
        queue_path,
        dry_run=False,
        session=session,
        now=clock,
        clips_dir=clips_dir,
        credentials=CREDENTIALS,
        pause=lambda seconds: None,
    )
    saved = json.loads(queue_path.read_text(encoding="utf8"))[0]
    # the clock is read when the entry starts (NOW) and again when its failure is recorded (NOW + 7 minutes)
    expected = NOW + timedelta(minutes=7 + instagram_schedule.RETRY_MINUTES)
    assert datetime.fromisoformat(saved["retry_after"]) == expected, saved


def test_a_lock_failure_that_is_not_contention_is_not_taken_for_another_pass():
    queue_path, clips_dir = make_setup()
    real_locking = msvcrt.locking

    def broken_locking(descriptor, mode, size):
        raise OSError(errno.EINVAL, "invalid argument")

    msvcrt.locking = broken_locking
    try:
        run_pass(queue_path, clips_dir, FakeSession())
    except OSError as error:
        assert error.errno == errno.EINVAL
        return
    finally:
        msvcrt.locking = real_locking
    raise AssertionError("a lock failure was reported as another pass running")


def test_watch_with_dry_run_runs_once():
    queue_path, clips_dir = make_setup()
    pauses = []
    instagram_schedule.watch(queue_path, True, pause=pauses.append, now=NOW, clips_dir=clips_dir)
    assert pauses == []


def test_the_two_authorization_headers_are_the_right_kind():
    queue_path, clips_dir = make_setup()
    session = FakeSession()
    run_pass(queue_path, clips_dir, session)
    upload = [call for call in session.calls if "rupload" in call[1]][0]
    assert upload[2]["headers"]["Authorization"] == f"OAuth {TOKEN}"
    graph = [call for call in session.calls if "rupload" not in call[1]]
    assert graph and all(call[2]["headers"]["Authorization"] == f"Bearer {TOKEN}" for call in graph)


def test_network_blips_do_not_use_up_the_attempts():
    queue_path, clips_dir = make_setup()

    def handler(method, url, kwargs):
        raise requests.ConnectionError("wifi is down")

    session = FakeSession(handler)
    for _ in range(instagram_schedule.MAX_ATTEMPTS + 2):
        saved = run_pass(queue_path, clips_dir, session)[0]
    assert "status" not in saved and not saved.get("attempts"), saved


def test_dead_container_is_parked_at_once_with_a_hint_how_to_retry():
    queue_path, clips_dir = make_setup({"container_id": "C1", "uploaded": True})
    session = FakeSession(
        lambda method, url, kwargs: {"id": "C1", "status_code": "EXPIRED"}
        if url.endswith("/C1") and "rupload" not in url
        else FakeSession.default_handler(method, url, kwargs)
    )
    records, logger = capture_logs()
    saved = run_pass(queue_path, clips_dir, session)[0]
    logger.removeHandler(records)
    assert saved["status"] == "needs_check" and saved["attempts"] == 1, saved
    assert any("delete status, attempts, error and retry_after" in line for line in records.lines), records.lines


def test_parked_entry_is_reported_every_pass_and_not_tried():
    queue_path, clips_dir = make_setup({"status": "needs_check", "error": "caption too long", "attempts": 3})
    session = FakeSession()
    records, logger = capture_logs()
    run_pass(queue_path, clips_dir, session)
    logger.removeHandler(records)
    assert session.calls == []
    assert any("needs_check" in line and "caption too long" in line for line in records.lines), records.lines


def test_entry_changed_by_hand_during_the_upload_is_not_published():
    queue_path, clips_dir = make_setup()

    def handler(method, url, kwargs):
        if "rupload" in url:
            queue = json.loads(queue_path.read_text(encoding="utf8"))
            queue[0]["publish_at"] = "2031-01-01T00:00:00+06:00"
            queue_path.write_text(json.dumps(queue), encoding="utf8")
        return FakeSession.default_handler(method, url, kwargs)

    session = FakeSession(handler)
    saved = run_pass(queue_path, clips_dir, session)
    assert not [call for call in session.calls if call[1].endswith("/media_publish")], (
        "published an entry the owner had changed"
    )
    assert saved[0]["publish_at"] == "2031-01-01T00:00:00+06:00" and saved[0].get("status") != "published", saved


def test_entry_removed_by_hand_during_a_pass_is_not_published():
    queue_path, clips_dir = make_setup(count=2)

    def handler(method, url, kwargs):
        if "rupload" in url:
            queue = json.loads(queue_path.read_text(encoding="utf8"))
            queue_path.write_text(json.dumps(queue[:1]), encoding="utf8")
        return FakeSession.default_handler(method, url, kwargs)

    session = FakeSession(handler)
    saved = run_pass(queue_path, clips_dir, session)
    assert len(media_calls(session)) == 1, "the removed entry was uploaded"
    assert [entry["caption"] for entry in saved] == ["Caption 0 #gta6"] and saved[0]["status"] == "published"


def test_a_second_pass_is_refused_while_the_first_holds_the_lock():
    queue_path, clips_dir = make_setup()
    session = FakeSession()
    records, logger = capture_logs()
    with held_lock(queue_path):
        result = run(
            queue_path,
            dry_run=False,
            session=session,
            now=NOW,
            clips_dir=clips_dir,
            credentials=CREDENTIALS,
            pause=lambda seconds: None,
        )
    logger.removeHandler(records)
    assert result is True and session.calls == []
    assert any("another" in line for line in records.lines), records.lines


def test_a_lock_file_left_by_a_crashed_pass_does_not_block():
    queue_path, clips_dir = make_setup()
    # a leftover file with nobody holding it: the operating system released the lock when that process died
    instagram_schedule.lock_path(queue_path).write_text("crashed", encoding="utf8")
    saved = run_pass(queue_path, clips_dir, FakeSession())[0]
    assert saved["status"] == "published"
    with instagram_schedule.queue_lock(queue_path) as got_lock:
        assert got_lock, "the lock was not released after the pass"


def test_dry_run_leaves_a_late_entry_unmarked():
    queue_path, clips_dir = make_setup(hours_from_now=-(instagram_schedule.MAX_LATE_HOURS + 1))
    before = queue_path.read_text(encoding="utf8")
    run_pass(queue_path, clips_dir, FakeSession(), dry_run=True)
    assert queue_path.read_text(encoding="utf8") == before


def test_token_is_only_sent_to_metas_upload_address():
    queue_path, clips_dir = make_setup({"container_id": "C1", "upload_url": "https://evil.example/steal"})
    session = FakeSession()
    saved = run_pass(queue_path, clips_dir, session)[0]
    assert not [call for call in session.calls if "evil.example" in call[1]]
    assert saved.get("status") != "published" and TOKEN not in json.dumps(saved)


def test_an_unconfirmed_upload_answer_for_a_container_meta_finished_is_taken_as_uploaded():
    queue_path, clips_dir = make_setup()
    # seen live five times on 2026-11-24: the upload answer said it did not confirm, yet the container was FINISHED
    session = FakeSession(
        lambda method, url, kwargs: {"debug_info": {"retriable": False, "message": "Request processing failed"}}
        if "rupload" in url
        else FakeSession.default_handler(method, url, kwargs)
    )
    saved = run_pass(queue_path, clips_dir, session)[0]
    assert saved.get("status") == "published" and saved.get("uploaded") is True and "attempts" not in saved, saved


def test_meta_request_limit_is_waited_out_and_never_counts_as_a_failed_attempt():
    queue_path, clips_dir = make_setup()
    session = FakeSession(
        lambda method, url, kwargs: {"error": {"code": 4, "message": "Application request limit reached"}}
    )
    wait = instagram_schedule.RATE_LIMIT_WAIT_MINUTES
    first = run_pass(queue_path, clips_dir, session)[0]
    assert "attempts" not in first and "status" not in first, first
    assert datetime.fromisoformat(first["retry_after"]) == NOW + timedelta(minutes=wait), first
    calls_after_first = len(session.calls)
    run_pass(queue_path, clips_dir, session, now=NOW + timedelta(minutes=wait - 1))
    assert len(session.calls) == calls_after_first, "tried again before the limit's waiting time was over"
    # many limited passes in a row must never park the reel
    for index in range(instagram_schedule.MAX_ATTEMPTS + 2):
        saved = run_pass(queue_path, clips_dir, session, now=NOW + timedelta(hours=index + 1))[0]
    assert "attempts" not in saved and saved.get("status") != "needs_check", saved


def test_other_meta_throttle_codes_are_waited_out_like_the_request_limit():
    for code in (17, 32, 613, 80002):
        queue_path, clips_dir = make_setup()
        session = FakeSession(lambda method, url, kwargs: {"error": {"code": code, "message": "too many calls"}})
        saved = run_pass(queue_path, clips_dir, session)[0]
        assert "attempts" not in saved and "status" not in saved and "retry_after" in saved, (code, saved)


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
            print("ok", name)
