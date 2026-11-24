import http.client
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import clip_server

TOKEN = "test-token"


class FakeProcess:
    def poll(self):
        return None


def start_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), clip_server.make_handler(TOKEN))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def call(port: int, method: str, path: str, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port)
    all_headers = {"X-Token": TOKEN}
    all_headers.update(headers or {})
    payload = json.dumps(body) if body is not None else None
    connection.request(method, path, body=payload, headers=all_headers)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, (json.loads(data) if data[:1] in (b"{", b"[") else data)


def test_wrong_token_gets_403():
    server, port = start_server()
    try:
        assert call(port, "GET", "/api/state", headers={"X-Token": "wrong"})[0] == 403
    finally:
        server.shutdown()


def test_missing_token_gets_403():
    server, port = start_server()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port)
        connection.request("GET", "/api/state")
        assert connection.getresponse().status == 403
    finally:
        server.shutdown()


def test_wrong_host_header_gets_403():
    server, port = start_server()
    try:
        assert call(port, "GET", "/api/state", headers={"Host": "evil.example"})[0] == 403
    finally:
        server.shutdown()


def test_state_returns_clips_and_presets():
    server, port = start_server()
    try:
        status, state = call(port, "GET", "/api/state")
        assert status == 200
        assert len(state["clips"]) >= 15 and "captions" in state["presets"] and state["upload"]["running"] is False
    finally:
        server.shutdown()


def test_second_upload_while_running_is_refused():
    server, port = start_server()
    started = []
    real_start = clip_server.start_upload
    clip_server.start_upload = lambda dry_run: started.append(dry_run) or FakeProcess()
    clip_server.UPLOAD["process"] = None
    try:
        assert call(port, "POST", "/api/upload", {"dry_run": True})[0] == 200
        assert call(port, "POST", "/api/upload", {"dry_run": True})[0] == 409
        assert started == [True]
    finally:
        clip_server.start_upload = real_start
        clip_server.UPLOAD["process"] = None
        server.shutdown()


def test_bad_clip_returns_400_with_problems():
    server, port = start_server()
    try:
        status, body = call(port, "POST", "/api/clip", {"name": "../x", "title": "T"})
        assert status == 400 and any("name" in problem for problem in body["problems"])
    finally:
        server.shutdown()


def test_update_unknown_clip_returns_400():
    server, port = start_server()
    try:
        status, body = call(port, "POST", "/api/clip/update", {"name": "no_such_clip", "caption": "speed"})
        assert status == 400 and any("no clip" in problem for problem in body["problems"])
    finally:
        server.shutdown()


def test_fast_double_upload_starts_one_process():
    import time

    server, port = start_server()
    started = []
    real_start = clip_server.start_upload

    def slow_start(dry_run):
        started.append(dry_run)
        time.sleep(0.3)
        return FakeProcess()

    clip_server.start_upload = slow_start
    clip_server.UPLOAD["process"] = None
    statuses = []
    try:
        threads = [threading.Thread(target=lambda: statuses.append(call(port, "POST", "/api/upload", {"dry_run": True})[0]))
                   for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(started) == 1 and statuses.count(200) == 1 and statuses.count(409) == 5, (started, statuses)
    finally:
        clip_server.start_upload = real_start
        clip_server.UPLOAD["process"] = None
        server.shutdown()


def test_file_writes_are_refused_while_an_upload_runs():
    server, port = start_server()
    clip_server.UPLOAD["process"] = FakeProcess()
    try:
        for path in ("/api/queue", "/api/clip", "/api/clip/update"):
            status, body = call(port, "POST", path, {"name": "x"})
            assert status == 409 and any("upload" in problem for problem in body["problems"]), path
    finally:
        clip_server.UPLOAD["process"] = None
        server.shutdown()


def test_bad_body_types_return_400_not_a_dropped_connection():
    server, port = start_server()
    try:
        assert call(port, "POST", "/api/clip", ["not", "an", "object"])[0] == 400
        assert call(port, "POST", "/api/render", ["x"])[0] == 400
        assert call(port, "POST", "/api/queue", {"file": 5, "title": "T", "description": "d", "publish_at": 7})[0] == 400
        assert call(port, "GET", "/api/frame?pick=tomo_lucia&t=abc")[0] == 400
    finally:
        server.shutdown()


def test_queue_file_must_be_an_mp4():
    server, port = start_server()
    try:
        entry = {"file": "clips/clips.json", "title": "T", "description": "d", "publish_at": "2099-01-01T10:00:00+06:00"}
        status, body = call(port, "POST", "/api/queue", entry)
        assert status == 400 and any("file" in problem for problem in body["problems"])
    finally:
        server.shutdown()


def test_facebook_routes_need_token_and_host():
    server, port = start_server()
    try:
        for method, path in (("POST", "/api/facebook/queue"), ("POST", "/api/facebook/upload"), ("GET", "/api/facebook/log")):
            assert call(port, method, path, {}, headers={"X-Token": "wrong"})[0] == 403, path
            assert call(port, method, path, {}, headers={"Host": "evil.example"})[0] == 403, path
    finally:
        server.shutdown()


def test_facebook_upload_refused_while_another_upload_runs():
    server, port = start_server()
    clip_server.UPLOAD["process"] = FakeProcess()
    try:
        status, body = call(port, "POST", "/api/facebook/upload", {"dry_run": True})
        assert status == 409 and any("upload" in problem for problem in body["problems"])
    finally:
        clip_server.UPLOAD["process"] = None
        server.shutdown()


def test_two_facebook_clicks_start_one_process():
    import time

    server, port = start_server()
    started = []
    real_start = clip_server.start_facebook_upload

    def slow_start(dry_run):
        started.append(dry_run)
        time.sleep(0.3)
        return FakeProcess()

    clip_server.start_facebook_upload = slow_start
    clip_server.UPLOAD["process"] = None
    statuses = []
    try:
        threads = [threading.Thread(target=lambda: statuses.append(call(port, "POST", "/api/facebook/upload", {"dry_run": True})[0]))
                   for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(started) == 1 and statuses.count(200) == 1 and statuses.count(409) == 5, (started, statuses)
    finally:
        clip_server.start_facebook_upload = real_start
        clip_server.UPLOAD["process"] = None
        server.shutdown()


def test_facebook_queue_write_refused_while_upload_runs():
    server, port = start_server()
    clip_server.UPLOAD["process"] = FakeProcess()
    try:
        status, body = call(port, "POST", "/api/facebook/queue", {"file": "x"})
        assert status == 409 and any("upload" in problem for problem in body["problems"])
    finally:
        clip_server.UPLOAD["process"] = None
        server.shutdown()


def test_facebook_queue_bad_entries_return_400():
    server, port = start_server()
    try:
        assert call(port, "POST", "/api/facebook/queue", ["list"])[0] == 400
        status, body = call(port, "POST", "/api/facebook/queue", {"file": "scripts/clip_server.py", "caption": "c", "publish_at": "2099-01-01T10:00:00+06:00"})
        assert status == 400 and any("file" in problem for problem in body["problems"])
    finally:
        server.shutdown()


def test_instagram_queue_route_refuses_bad_entries_and_state_offers_free_slots():
    server, port = start_server()
    try:
        assert call(port, "POST", "/api/instagram/queue", ["list"])[0] == 400
        status, body = call(port, "POST", "/api/instagram/queue", {"file": "scripts/clip_server.py", "caption": "c", "publish_at": "2099-01-01T10:00:00+06:00"})
        assert status == 400 and any("file" in problem for problem in body["problems"])
        assert "instagram_slots" in call(port, "GET", "/api/state")[1]
    finally:
        server.shutdown()


def test_state_reports_the_progress_of_a_running_render():
    server, port = start_server()
    name = clip_server.clip_store.load_clips()[0]["name"]
    real_progress = clip_server.render_progress
    clip_server.RENDERS[name] = "running"
    clip_server.render_progress = lambda clip: 0.25
    try:
        assert call(port, "GET", "/api/state")[1]["render_progress"] == {name: 0.25}
    finally:
        clip_server.render_progress = real_progress
        clip_server.RENDERS.pop(name, None)
        server.shutdown()


def test_state_has_facebook_parts_and_no_token():
    server, port = start_server()
    try:
        status, state = call(port, "GET", "/api/state")
        assert status == 200 and "facebook_queue" in state and "facebook_slots" in state
        dumped = json.dumps(state)
        # the secrets themselves, not the word: a clip about a game token legitimately says "token"
        assert TOKEN not in dumped and "page_access_token" not in dumped and "Bearer" not in dumped
    finally:
        server.shutdown()


def with_instagram_queue(content):
    # points the server at a temporary Instagram queue file (None = no file) and returns what /api/state shows
    import tempfile
    import instagram_schedule
    folder = tempfile.TemporaryDirectory()
    path = Path(folder.name) / "instagram_queue.json"
    if content is not None:
        path.write_text(content, encoding="utf8")
    real = instagram_schedule.DEFAULT_QUEUE
    instagram_schedule.DEFAULT_QUEUE = path
    server, port = start_server()
    try:
        return call(port, "GET", "/api/state")
    finally:
        server.shutdown()
        instagram_schedule.DEFAULT_QUEUE = real
        folder.cleanup()


def test_state_shows_the_instagram_queue_without_upload_addresses_or_ids():
    queue = [{"file": "clips/speed/ig_a.mp4", "caption": "long caption", "publish_at": "2026-11-25T06:00:00+06:00",
              "status": "needs_check", "attempts": 4, "error": "Meta said no", "container_id": "123",
              "upload_url": "https://rupload.facebook.com/secret"}]
    status, state = with_instagram_queue(json.dumps(queue))
    assert status == 200
    assert state["instagram_queue"] == [{"file": "clips/speed/ig_a.mp4", "publish_at": "2026-11-25T06:00:00+06:00",
                                         "status": "needs_check", "attempts": 4, "error": "Meta said no"}]
    assert "rupload" not in json.dumps(state) and "container_id" not in json.dumps(state)


def test_state_still_loads_when_the_instagram_queue_is_missing_or_broken():
    for content in (None, "{not json", '["a string", 5]', "5", "null", '{"file": "x"}'):
        status, state = with_instagram_queue(content)
        assert status == 200 and state["instagram_queue"] == []


def test_text_route_youtube_and_facebook():
    server, port = start_server()
    try:
        status, youtube = call(port, "GET", "/api/text?clip=kaip1_4_bigfoot&platform=youtube")
        assert status == 200 and youtube["title"].startswith("Kai Cenat is hunting") and "#shorts" in youtube["description"]
        status, facebook = call(port, "GET", "/api/text?clip=kaip1_4_bigfoot&platform=facebook")
        assert status == 200 and facebook["caption"].startswith(youtube["title"])
    finally:
        server.shutdown()


def test_text_route_rejects_bad_platform_and_unknown_clip():
    server, port = start_server()
    try:
        assert call(port, "GET", "/api/text?clip=kaip1_4_bigfoot&platform=tiktok")[0] == 400
        assert call(port, "GET", "/api/text?clip=no_such_clip&platform=youtube")[0] == 400
        assert call(port, "GET", "/api/text?platform=youtube")[0] == 400
    finally:
        server.shutdown()


def test_text_route_needs_token():
    server, port = start_server()
    try:
        assert call(port, "GET", "/api/text?clip=kaip1_4_bigfoot&platform=youtube", headers={"X-Token": "x"})[0] == 403
    finally:
        server.shutdown()


PRESET_BODY = {"kind": "captions", "name": "loud", "preset": {
    "font": "Arial", "size": 14, "primary": "&H0000FFFF", "outline_colour": "&H00000000", "outline": 3, "margin_v": 40,
    "title_colour": "white", "bottom_colour": "yellow"}}


def test_preset_route_saves_a_good_style_to_a_copy_and_returns_it_in_state():
    import tempfile

    import clip_store

    real_path = clip_store.PRESETS_PATH
    with tempfile.TemporaryDirectory() as folder:
        clip_store.PRESETS_PATH = Path(folder) / "presets.json"
        clip_store.PRESETS_PATH.write_text(real_path.read_text(encoding="utf8"), encoding="utf8")
        server, port = start_server()
        try:
            assert call(port, "POST", "/api/preset", PRESET_BODY) == (200, {"problems": []})
            assert call(port, "GET", "/api/state")[1]["presets"]["captions"]["loud"]["size"] == 14
        finally:
            server.shutdown()
            clip_store.PRESETS_PATH = real_path


def test_preset_route_refuses_bad_styles_and_needs_token_and_no_upload_running():
    server, port = start_server()
    try:
        bad = dict(PRESET_BODY, preset=dict(PRESET_BODY["preset"], font="Arial,Outline=9"))
        status, body = call(port, "POST", "/api/preset", bad)
        assert status == 400 and any("font" in problem for problem in body["problems"])
        assert call(port, "POST", "/api/preset", {"kind": "captions"})[0] == 400
        assert call(port, "POST", "/api/preset", PRESET_BODY, headers={"X-Token": "x"})[0] == 403
        clip_server.UPLOAD["process"] = FakeProcess()
        assert call(port, "POST", "/api/preset", PRESET_BODY)[0] == 409
    finally:
        clip_server.UPLOAD["process"] = None
        server.shutdown()


def test_thumb_route_makes_a_cached_jpeg_for_a_rendered_clip():
    import tempfile

    real_dir = clip_server.THUMBS_DIR
    with tempfile.TemporaryDirectory() as folder:
        clip_server.THUMBS_DIR = Path(folder) / "thumbs"
        server, port = start_server()
        try:
            status, data = call(port, "GET", "/api/thumb?clip=kaip1_4_bigfoot")
            assert status == 200 and data[:2] == b"\xff\xd8"
            assert (Path(folder) / "thumbs" / "kaip1_4_bigfoot.jpg").is_file()
            assert call(port, "GET", "/api/thumb?clip=kaip1_4_bigfoot")[0] == 200
        finally:
            server.shutdown()
            clip_server.THUMBS_DIR = real_dir


def test_many_requests_for_one_uncached_thumbnail_all_get_a_picture():
    import tempfile

    real_dir = clip_server.THUMBS_DIR
    with tempfile.TemporaryDirectory() as folder:
        clip_server.THUMBS_DIR = Path(folder) / "thumbs"
        results = []
        threads = [threading.Thread(target=lambda: results.append(clip_server.thumbnail("kaip1_4_bigfoot"))) for _ in range(8)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            jpeg_start = bytes([255, 216])
            assert len(results) == 8 and all(picture and picture[:2] == jpeg_start for picture in results), results
        finally:
            clip_server.THUMBS_DIR = real_dir


def test_the_same_clip_never_gets_two_thumbnail_jobs_at_once():
    import tempfile
    import time

    real_dir, real_make = clip_server.THUMBS_DIR, clip_server.make_thumbnail
    running = {"now": 0, "most": 0}
    guard = threading.Lock()

    def slow_make(video, thumb):
        with guard:
            running["now"] += 1
            running["most"] = max(running["most"], running["now"])
        time.sleep(0.2)
        thumb.write_bytes(b"picture")
        with guard:
            running["now"] -= 1
        return True

    with tempfile.TemporaryDirectory() as folder:
        clip_server.THUMBS_DIR = Path(folder) / "thumbs"
        clip_server.THUMBS_DIR.mkdir()
        clip_server.make_thumbnail = slow_make
        results = []
        threads = [threading.Thread(target=lambda: results.append(clip_server.thumbnail("kaip1_4_bigfoot"))) for _ in range(6)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            # two jobs on one file made one request replace the picture while another was reading it (a PermissionError on Windows)
            assert running["most"] == 1, running
            assert results == [b"picture"] * 6, results
        finally:
            clip_server.make_thumbnail = real_make
            clip_server.THUMBS_DIR = real_dir


def test_a_failed_thumbnail_run_returns_nothing_and_ignores_a_leftover_file():
    import subprocess
    import tempfile

    real_dir, real_run = clip_server.THUMBS_DIR, clip_server.subprocess.run
    with tempfile.TemporaryDirectory() as folder:
        clip_server.THUMBS_DIR = Path(folder) / "thumbs"
        clip_server.THUMBS_DIR.mkdir()
        (clip_server.THUMBS_DIR / "kaip1_4_bigfoot.tmp.jpg").write_bytes(b"garbage from a killed run")
        clip_server.subprocess.run = lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, b"", b"broken")
        try:
            assert clip_server.thumbnail("kaip1_4_bigfoot") is None
            assert not (clip_server.THUMBS_DIR / "kaip1_4_bigfoot.jpg").exists()
        finally:
            clip_server.subprocess.run = real_run
            clip_server.THUMBS_DIR = real_dir


def test_a_hung_thumbnail_run_times_out_and_frees_its_slot():
    import subprocess
    import tempfile

    real_dir, real_run = clip_server.THUMBS_DIR, clip_server.subprocess.run
    slots = clip_server.THUMB_JOBS._value

    def hang(*args, **kwargs):
        assert kwargs.get("timeout"), "ffmpeg must run with a timeout"
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    with tempfile.TemporaryDirectory() as folder:
        clip_server.THUMBS_DIR = Path(folder) / "thumbs"
        clip_server.subprocess.run = hang
        try:
            assert clip_server.thumbnail("kaip1_4_bigfoot") is None
            assert clip_server.THUMB_JOBS._value == slots
        finally:
            clip_server.subprocess.run = real_run
            clip_server.THUMBS_DIR = real_dir


def test_server_keeps_a_long_connection_queue_and_few_thumbnail_jobs():
    # a cold library page asks for many pictures at once; a short queue refused some connections
    assert clip_server.ClipServer.request_queue_size >= 64
    assert clip_server.ClipServer.daemon_threads is True
    assert 1 <= clip_server.THUMB_JOBS._value <= 4


def test_thumb_route_refuses_unknown_clips_paths_and_missing_token():
    server, port = start_server()
    try:
        for name in ("no_such_clip", "..%2Fclips", ""):
            assert call(port, "GET", f"/api/thumb?clip={name}")[0] == 404, name
        assert call(port, "GET", "/api/thumb?clip=kaip1_4_bigfoot", headers={"X-Token": "x"})[0] == 403
    finally:
        server.shutdown()


CREATOR_BODY = {"name": "mrbeast", "creator": {"youtube": ["mrbeast", "gaming"], "facebook": ["fyp"],
                                               "caption": "default", "layout": "tomo"}}


def test_creator_route_saves_to_a_copy_and_state_lists_the_creator():
    import tempfile

    import clip_store

    real_path = clip_store.PRESETS_PATH
    with tempfile.TemporaryDirectory() as folder:
        clip_store.PRESETS_PATH = Path(folder) / "presets.json"
        clip_store.PRESETS_PATH.write_text(real_path.read_text(encoding="utf8"), encoding="utf8")
        server, port = start_server()
        try:
            assert call(port, "POST", "/api/creator", CREATOR_BODY) == (200, {"problems": []})
            creators = call(port, "GET", "/api/state")[1]["presets"]["creators"]
            assert creators["mrbeast"]["youtube"] == ["mrbeast", "gaming"] and "tomo" in creators
        finally:
            server.shutdown()
            clip_store.PRESETS_PATH = real_path


def test_creator_route_refuses_bad_creators_needs_token_and_no_upload_running():
    server, port = start_server()
    try:
        bad = dict(CREATOR_BODY, creator=dict(CREATOR_BODY["creator"], layout="nope"))
        status, body = call(port, "POST", "/api/creator", bad)
        assert status == 400 and any("layout" in problem for problem in body["problems"])
        assert call(port, "POST", "/api/creator", {"name": "x"})[0] == 400
        assert call(port, "POST", "/api/creator", CREATOR_BODY, headers={"X-Token": "x"})[0] == 403
        clip_server.UPLOAD["process"] = FakeProcess()
        assert call(port, "POST", "/api/creator", CREATOR_BODY)[0] == 409
    finally:
        clip_server.UPLOAD["process"] = None
        server.shutdown()


def test_queue_file_outside_clips_is_refused():
    server, port = start_server()
    try:
        entry = {"file": "scripts/clip_server.py", "title": "T", "description": "d", "publish_at": "2099-01-01T10:00:00+06:00"}
        status, body = call(port, "POST", "/api/queue", entry)
        assert status == 400 and any("file" in problem for problem in body["problems"])
    finally:
        server.shutdown()



class SourceWorkspace:
    # a temp sources folder and fake workers, so no network, yt-dlp or Whisper runs
    GOOD_URL = "https://www.youtube.com/watch?v=UeHMwf9vIss"

    def __enter__(self):
        import tempfile

        import sources

        self.folder = tempfile.TemporaryDirectory()
        self.saved = {name: getattr(clip_server, name) for name in
                      ("SOURCES_ROOT", "add_source", "scan_source", "approve_candidate", "queue_clip")}
        clip_server.SOURCES_ROOT = Path(self.folder.name)
        clip_server.add_source = lambda url, creator, root: sources.add_source(
            url, creator, fetch=lambda link: {"title": "A stream", "duration": 600}, root=root)
        clip_server.JOBS.clear()
        self.sources = sources
        return Path(self.folder.name)

    def __exit__(self, *error):
        for name, value in self.saved.items():
            setattr(clip_server, name, value)
        clip_server.JOBS.clear()
        clip_server.UPLOAD["process"] = None
        self.folder.cleanup()


def add_good_source(port: int):
    status, body = call(port, "POST", "/api/source", {"url": SourceWorkspace.GOOD_URL, "creator": "speed"})
    assert status == 200, body
    return body["source"]["id"]


def wait_for_job(key: str, wanted: str):
    import time

    for _ in range(100):
        if clip_server.JOBS.get(key, "").startswith(wanted):
            return
        time.sleep(0.05)
    raise AssertionError(f"{key}: {clip_server.JOBS.get(key)}")


def test_source_route_adds_a_source_and_state_lists_it():
    with SourceWorkspace():
        server, port = start_server()
        try:
            source_id = add_good_source(port)
            sources = call(port, "GET", "/api/state")[1]["sources"]
            assert [source["id"] for source in sources] == [source_id] and sources[0]["candidates"] == []
        finally:
            server.shutdown()


def test_source_route_refuses_bad_urls_unknown_creators_and_missing_token():
    with SourceWorkspace():
        server, port = start_server()
        try:
            status, body = call(port, "POST", "/api/source", {"url": "file:///x", "creator": "speed"})
            assert status == 400 and "YouTube" in body["problems"][0]
            status, body = call(port, "POST", "/api/source", {"url": SourceWorkspace.GOOD_URL, "creator": "nobody"})
            assert status == 400 and "creator" in body["problems"][0]
            assert call(port, "POST", "/api/source", {"url": SourceWorkspace.GOOD_URL, "creator": "speed"},
                        headers={"X-Token": "wrong"})[0] == 403
        finally:
            server.shutdown()


def test_a_second_scan_while_one_runs_is_refused_and_the_job_status_reaches_the_page():
    release = threading.Event()
    with SourceWorkspace():
        clip_server.scan_source = lambda source_id, root: release.wait(5) and []
        server, port = start_server()
        try:
            source_id = add_good_source(port)
            assert call(port, "POST", "/api/source/scan", {"id": source_id}) == (200, {"started": source_id})
            assert call(port, "POST", "/api/source/scan", {"id": source_id})[0] == 409
            assert call(port, "GET", "/api/state")[1]["sources"][0]["job"] == "running"
            release.set()
            wait_for_job(f"scan:{source_id}", "done")
            assert call(port, "POST", "/api/source/scan", {"id": "../x"})[0] == 400
        finally:
            release.set()
            server.shutdown()


def test_a_failed_scan_reports_its_reason_in_the_job_status():
    def failing(source_id, root):
        raise RuntimeError("no audio")

    with SourceWorkspace():
        clip_server.scan_source = failing
        server, port = start_server()
        try:
            source_id = add_good_source(port)
            call(port, "POST", "/api/source/scan", {"id": source_id})
            wait_for_job(f"scan:{source_id}", "failed")
            assert "no audio" in clip_server.JOBS[f"scan:{source_id}"]
        finally:
            server.shutdown()


def write_candidates(root: Path, source_id: str):
    candidate = {"id": "c40", "peak": 100, "loudness": -8.0, "start": 40, "end": 130, "text": "oh no"}
    (root / source_id / "candidates.json").write_text(json.dumps([candidate]), encoding="utf8")
    (root / source_id / "win").mkdir()
    (root / source_id / "win" / "w40.wav").write_bytes(b"RIFFwave")


APPROVAL_BODY = {"candidate": "c40", "name": "speed_9_oh_no", "title": "SPEED OH NO", "hook": "Speed said oh no", "from": 30, "to": 75}


def test_approve_route_runs_the_cut_with_the_stored_candidate_not_the_request_values():
    calls = []
    with SourceWorkspace() as root:
        clip_server.approve_candidate = lambda source, candidate, approval: calls.append((source, candidate, approval)) or {}
        server, port = start_server()
        try:
            source_id = add_good_source(port)
            write_candidates(root, source_id)
            body = dict(APPROVAL_BODY, source=source_id, start=1, end=2)
            assert call(port, "POST", "/api/candidate/approve", body)[0] == 200
            wait_for_job(f"approve:{source_id}:c40", "done")
            assert calls[0][1]["start"] == 40 and calls[0][0]["creator"] == "speed"
            assert call(port, "POST", "/api/candidate/approve", dict(body, candidate="c999"))[0] == 400
            assert call(port, "POST", "/api/candidate/approve", dict(body, source="../x"))[0] == 400
        finally:
            server.shutdown()


def test_approve_and_autoqueue_are_refused_while_an_upload_runs():
    with SourceWorkspace() as root:
        server, port = start_server()
        try:
            source_id = add_good_source(port)
            write_candidates(root, source_id)
            clip_server.UPLOAD["process"] = FakeProcess()
            assert call(port, "POST", "/api/candidate/approve", dict(APPROVAL_BODY, source=source_id))[0] == 409
            assert call(port, "POST", "/api/clip/autoqueue", {"name": "x"})[0] == 409
        finally:
            server.shutdown()


def test_approve_job_failure_is_reported_and_marks_the_clip_failed():
    def failing(source, candidate, approval):
        raise ValueError("title: 1 to 28 characters")

    with SourceWorkspace() as root:
        clip_server.approve_candidate = failing
        server, port = start_server()
        try:
            source_id = add_good_source(port)
            write_candidates(root, source_id)
            call(port, "POST", "/api/candidate/approve", dict(APPROVAL_BODY, source=source_id))
            wait_for_job(f"approve:{source_id}:c40", "failed")
            assert "title" in clip_server.JOBS[f"approve:{source_id}:c40"]
        finally:
            server.shutdown()


def test_autoqueue_route_returns_the_slots_or_the_problem():
    with SourceWorkspace():
        server, port = start_server()
        try:
            clip_server.queue_clip = lambda name: {"youtube": "2099-01-01T10:00:00+06:00", "facebook": "2099-01-01T10:00:00+06:00"}
            status, body = call(port, "POST", "/api/clip/autoqueue", {"name": "x"})
            assert status == 200 and body["slots"]["youtube"].startswith("2099")

            def refusing(name):
                raise ValueError("'x' is already in a queue")

            clip_server.queue_clip = refusing
            status, body = call(port, "POST", "/api/clip/autoqueue", {"name": "x"})
            assert status == 400 and "already" in body["problems"][0]
        finally:
            server.shutdown()


def test_window_route_serves_the_candidate_audio_and_refuses_paths():
    with SourceWorkspace() as root:
        server, port = start_server()
        try:
            source_id = add_good_source(port)
            write_candidates(root, source_id)
            status, data = call(port, "GET", f"/api/window?source={source_id}&candidate=c40")
            assert status == 200 and data == b"RIFFwave"
            for query in ("source=../x&candidate=c40", f"source={source_id}&candidate=c999", f"source={source_id}&candidate=../c40"):
                assert call(port, "GET", f"/api/window?{query}")[0] == 404, query
            assert call(port, "GET", f"/api/window?source={source_id}&candidate=c40", headers={"X-Token": "wrong"})[0] == 403
        finally:
            server.shutdown()


def test_a_failed_command_job_reports_the_program_and_code_not_the_whole_command_line():
    import subprocess

    def failing(source_id, root):
        raise subprocess.CalledProcessError(1, ["D:/Tools/yt-dlp/yt-dlp.exe", "--secret-looking", "https://x"])

    with SourceWorkspace():
        clip_server.scan_source = failing
        server, port = start_server()
        try:
            source_id = add_good_source(port)
            call(port, "POST", "/api/source/scan", {"id": source_id})
            wait_for_job(f"scan:{source_id}", "failed")
            reason = clip_server.JOBS[f"scan:{source_id}"]
            assert "yt-dlp.exe failed with code 1" in reason and "--secret-looking" not in reason
        finally:
            server.shutdown()


def test_a_timed_out_job_reports_the_program_and_the_seconds():
    import subprocess

    def hanging(source_id, root):
        raise subprocess.TimeoutExpired(["D:/Tools/yt-dlp/yt-dlp.exe", "--long"], 3600)

    with SourceWorkspace():
        clip_server.scan_source = hanging
        server, port = start_server()
        try:
            source_id = add_good_source(port)
            call(port, "POST", "/api/source/scan", {"id": source_id})
            wait_for_job(f"scan:{source_id}", "failed")
            reason = clip_server.JOBS[f"scan:{source_id}"]
            assert "yt-dlp.exe timed out after 3600 s" in reason and "--long" not in reason
        finally:
            server.shutdown()


def test_state_names_the_clip_already_cut_from_a_candidate():
    import tempfile

    import clip_store

    real_path = clip_store.CLIPS_PATH
    with SourceWorkspace() as root, tempfile.TemporaryDirectory() as folder:
        clip_store.CLIPS_PATH = Path(folder) / "clips.json"
        clip_store.CLIPS_PATH.write_text(json.dumps([{"name": "speed_9_oh_no", "streamer": "speed", "pick": "v_uehmwf9viss_c40_28_77"}]),
                                         encoding="utf8")
        server, port = start_server()
        try:
            source_id = add_good_source(port)
            write_candidates(root, source_id)
            candidate = call(port, "GET", "/api/state")[1]["sources"][0]["candidates"][0]
            assert candidate["clip"] == "speed_9_oh_no"
        finally:
            clip_store.CLIPS_PATH = real_path
            server.shutdown()


def test_captions_route_lists_the_lines_a_clip_will_show_and_refuses_unknown_clips():
    server, port = start_server()
    try:
        status, data = call(port, "GET", "/api/captions?clip=tomo_4_cash_slider")
        assert status == 200 and data["captions"] and all({"start", "end", "text"} <= set(line) for line in data["captions"])
        assert call(port, "GET", "/api/captions?clip=nope")[0] == 400
    finally:
        server.shutdown()


def raw_call(port: int, path: str, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port)
    connection.request("GET", path, headers=dict({"X-Token": TOKEN}, **(headers or {})))
    response = connection.getresponse()
    data = response.read()
    result = (response.status, dict(response.getheaders()), data)
    connection.close()
    return result


def test_video_route_serves_the_rendered_clip_with_byte_ranges_so_the_page_can_seek():
    server, port = start_server()
    try:
        real = (Path("clips") / "tomo" / "tomo_4_cash_slider.mp4").read_bytes()
        status, headers, data = raw_call(port, "/api/video?clip=tomo_4_cash_slider", {"Range": "bytes=100-199"})
        assert status == 206 and data == real[100:200]
        assert headers["Content-Range"] == f"bytes 100-199/{len(real)}" and headers["Accept-Ranges"] == "bytes"
        status, headers, data = raw_call(port, "/api/video?clip=tomo_4_cash_slider", {"Range": f"bytes={len(real) - 10}-"})
        assert status == 206 and data == real[-10:]
        status, headers, data = raw_call(port, "/api/video?clip=tomo_4_cash_slider")
        assert status == 206 and data == real[:len(data)] and headers["Content-Range"].startswith("bytes 0-")
        assert raw_call(port, "/api/video?clip=nope")[0] == 404
        assert raw_call(port, "/api/video?clip=../clips/clips")[0] == 404
        assert raw_call(port, f"/api/video?clip=tomo_4_cash_slider", {"Range": f"bytes={len(real) + 5}-"})[0] == 416
    finally:
        server.shutdown()


def with_copied_library():
    import shutil
    import tempfile

    import clip_store

    folder = tempfile.mkdtemp()
    real_path = clip_store.CLIPS_PATH
    clip_store.CLIPS_PATH = Path(folder) / "clips.json"
    shutil.copy(real_path, clip_store.CLIPS_PATH)
    return real_path, folder


def test_find_camera_route_runs_the_face_finder_and_saves_the_clips_crop():
    import shutil

    import clip_store

    real_path, folder = with_copied_library()
    server, port = start_server()
    try:
        status, data = call(port, "POST", "/api/clip/camera", {"name": "speed_1_crash_99"})
        assert status == 200 and data == {"problems": [], "notes": []}
        crop = next(c for c in clip_store.load_clips() if c["name"] == "speed_1_crash_99")["cam_crop"]
        assert crop.startswith("crop=388:240:")
        assert call(port, "POST", "/api/clip/camera", {"name": "nope"})[0] == 400
    finally:
        server.shutdown()
        clip_store.CLIPS_PATH = real_path
        shutil.rmtree(folder, ignore_errors=True)


def test_find_camera_route_says_so_when_no_face_is_near_the_usual_spot():
    import shutil

    import clip_store

    real_path, folder = with_copied_library()
    real_finder = clip_server.find_clip_crop
    clip_server.find_clip_crop = lambda *arguments: None
    server, port = start_server()
    try:
        status, data = call(port, "POST", "/api/clip/camera", {"name": "speed_1_crash_99"})
        assert status == 400 and "no face" in data["problems"][0]
        assert "cam_crop" not in next(c for c in clip_store.load_clips() if c["name"] == "speed_1_crash_99")
    finally:
        server.shutdown()
        clip_server.find_clip_crop = real_finder
        clip_store.CLIPS_PATH = real_path
        shutil.rmtree(folder, ignore_errors=True)


def run_find_camera(finder):
    import shutil

    import clip_store

    real_path, folder = with_copied_library()
    real_finder = clip_server.find_clip_crop
    clip_server.find_clip_crop = finder
    server, port = start_server()
    try:
        status, data = call(port, "POST", "/api/clip/camera", {"name": "speed_1_crash_99"})
        return status, data, next(c for c in clip_store.load_clips() if c["name"] == "speed_1_crash_99")
    finally:
        server.shutdown()
        clip_server.find_clip_crop = real_finder
        clip_store.CLIPS_PATH = real_path
        shutil.rmtree(folder, ignore_errors=True)


def test_find_camera_samples_three_moments_and_trusts_a_camera_that_stays_put():
    seen = []
    status, data, clip = run_find_camera(lambda pick, second, box, margin=0.5: seen.append(second) or "crop=388:240:13:419")
    assert status == 200 and data["notes"] == [] and clip["cam_crop"] == "crop=388:240:13:419" and not clip.get("cam_unverified")
    assert len(seen) == 3 and seen == sorted(seen)


def test_find_camera_flags_a_camera_that_moves_during_the_clip():
    crops = iter(["crop=388:240:0:461", "crop=388:240:600:100", "crop=388:240:0:461"])
    status, data, clip = run_find_camera(lambda pick, second, box, margin=0.5: next(crops))
    assert status == 200 and "moves" in data["notes"][0] and clip["cam_unverified"] is True


def test_find_camera_falls_back_to_a_face_far_away_and_flags_it():
    status, data, clip = run_find_camera(lambda pick, second, box, margin=0.5: None if margin == 0.5 else "crop=388:240:900:300")
    assert status == 200 and "far" in data["notes"][0] and clip["cam_crop"] == "crop=388:240:900:300" and clip["cam_unverified"] is True


def test_find_camera_with_no_face_anywhere_changes_nothing():
    status, data, clip = run_find_camera(lambda *arguments, **keywords: None)
    assert status == 400 and "no face" in data["problems"][0] and "cam_crop" not in clip


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
            print("ok", name)
