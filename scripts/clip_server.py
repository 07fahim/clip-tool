"""Local clip page: lists clips, renders them with chosen presets, keeps the YouTube queue, starts uploads.
Usage (youtube env): D:/Tools/youtube-env/Scripts/python scripts/clip_server.py
Prints a link with a one-time token; only this computer can reach it."""
import json
import logging
import math
import os
import re
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import clip_store
from camera import find_clip_crop, parse_crop
import facebook_queue
import facebook_schedule
import instagram_queue
import instagram_schedule
import queue_store
from cut import approve_candidate, check_approval, queue_clip
from detect import load_candidates, scan_source
from make_gta_clips import CLIPS_DIR, PICKS, clip_captions, make_clip, render_progress
from sources import SOURCES_DIR, add_source, load_sources
from youtube_schedule import DEFAULT_QUEUE, LOG_PATH

PAGE_PATH = Path(__file__).with_name("clip_page.html")
LOG_LINES_SHOWN = 40
# upload log line written by youtube_schedule.authorize when the 7-day login has run out
LOGIN_EXPIRED_TEXT = "login expired"
SLOTS_SHOWN = 5
INSTAGRAM_SHOWN = ("file", "publish_at", "status", "uploaded", "attempts", "error", "retry_after")
# small pictures of rendered clips for the library; made on first request, made again when the clip is rendered again
THUMBS_DIR = CLIPS_DIR / ".thumbs"
THUMB_WIDTH = 270
THUMB_TIMEOUT_SECONDS = 30
THUMB_LOCKS = {}
THUMB_LOCKS_GUARD = threading.Lock()
# a cold library page asks for every picture at once; running them all at once starved the server of CPU and
# some connections were refused (seen with 15 clips), so only a few run together
THUMB_JOBS = threading.Semaphore(2)

logger = logging.getLogger("clip_server")
# the one running upload process, and the render status of each clip name; shared by all requests
UPLOAD = {"process": None}
RENDERS = {}
RENDER_LOCK = threading.Lock()
# makes "is one running?" and "start one" a single step, so two fast clicks start one process
UPLOAD_LOCK = threading.Lock()
UPLOAD_RUNNING_PROBLEM = "an upload is running; the queue and clip files are locked until it finishes"
# source videos and their candidates live here; tests point it at a temp folder
SOURCES_ROOT = SOURCES_DIR
# status of each background job by key, "scan:<source>" or "approve:<source>:<candidate>": running, done or failed: why
JOBS = {}
JOBS_LOCK = threading.Lock()
MAX_REASON_LENGTH = 200
# the video is sent in pieces like this: the page asks for the next piece when it needs it, so a clip never sits whole in memory
VIDEO_CHUNK_BYTES = 4 * 1024 * 1024
# where in the clip's first segment the face finder looks: a blink or a zoom at one moment must not decide, and a camera that
# moved shows as different crops
CAMERA_SAMPLE_POINTS = (0.25, 0.5, 0.75)
# when no face is near the usual spot, look this many box sizes around it (3 reaches most of a 1920x1080 frame from a corner box)
FAR_SEARCH_MARGIN = 3


def upload_running():
    return UPLOAD["process"] is not None and UPLOAD["process"].poll() is None


def start_upload(dry_run: bool):
    command = [sys.executable, "scripts/youtube_schedule.py"] + (["--dry-run"] if dry_run else [])
    return subprocess.Popen(command)


def start_facebook_upload(dry_run: bool):
    command = [sys.executable, "scripts/facebook_schedule.py"] + (["--dry-run"] if dry_run else [])
    return subprocess.Popen(command)


def log_tail(log_path: Path = LOG_PATH):
    if not log_path.exists():
        return []
    return log_path.read_text(encoding="utf8").splitlines()[-LOG_LINES_SHOWN:]


def render_in_background(clip: dict):
    try:
        make_clip(clip)
        RENDERS[clip["name"]] = "done"
    except Exception as error:
        RENDERS[clip["name"]] = f"failed: {error}"
        logger.error(f"render '{clip['name']}' failed: {error}")


def start_render(name: str):
    clip = next((clip for clip in clip_store.load_clips() if clip["name"] == name), None)
    if clip is None:
        return 404, {"problems": [f"no clip named '{name}'"]}
    with RENDER_LOCK:
        if RENDERS.get(name) == "running":
            return 409, {"problems": [f"'{name}' is already rendering"]}
        RENDERS[name] = "running"
    threading.Thread(target=render_in_background, args=(clip,), daemon=True).start()
    return 200, {"started": name}


def queue_file_problems(entry: dict):
    file_name = entry.get("file")
    if not isinstance(file_name, str) or not file_name.endswith(".mp4"):
        return ["file: must be an .mp4 video inside the clips folder"]
    if CLIPS_DIR.resolve() not in Path(file_name).resolve().parents:
        return ["file: must be an .mp4 video inside the clips folder"]
    return []


def frame_png(pick: str, seconds: float):
    pick_path = PICKS / f"{pick}.mp4"
    if not clip_store.is_plain_name(pick) or not pick_path.is_file():
        return None
    command = ["ffmpeg", "-v", "error", "-ss", str(seconds), "-i", str(pick_path), "-frames:v", "1",
               "-f", "image2", "-c:v", "png", "pipe:1"]
    return subprocess.run(command, capture_output=True).stdout


def video_range(header, size: int):
    # (first, last) byte for a Range header, or None when it asks for bytes past the end; no header means from the start
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header or "")
    first, last = match.groups() if match else ("", "")
    if first == "" and last != "":
        start, end = max(size - int(last), 0), size - 1
    else:
        start, end = int(first or 0), int(last) if last else size - 1
    end = min(end, size - 1, start + VIDEO_CHUNK_BYTES - 1)
    return (start, end) if 0 <= start <= end else None


def find_camera(name: str) -> dict:
    # runs the face finder at three moments of the clip's first segment, near where the streamer's own layout puts the camera,
    # and saves a crop as the clip's camera crop. The clip is marked "check the camera" when the camera seems to move or the
    # only face found was far from the usual spot. Returns the problems (empty when it worked) and notes for the owner
    clip = next((clip for clip in clip_store.load_clips() if clip["name"] == name), None)
    if clip is None:
        return {"problems": [f"no clip named '{name}'"], "notes": []}
    presets = clip_store.load_presets()
    look = presets.get("creators", {}).get(clip["streamer"])
    box = look and presets["layouts"].get(look["layout"], {}).get("cam_crop")
    if not box:
        return {"problems": [f"{clip['streamer']} has no layout with a camera position to search near: set the crop by hand"], "notes": []}
    start, end = clip["segments"][0]
    pick = PICKS / f"{clip['pick']}.mp4"
    seconds = [start + (end - start) * point for point in CAMERA_SAMPLE_POINTS]
    notes = []
    near = [crop for crop in (find_clip_crop(pick, second, box) for second in seconds) if crop]
    if near:
        crop = near[len(near) // 2]
        xs, ys = [parse_crop(found)[0] for found in near], [parse_crop(found)[1] for found in near]
        _, _, box_width, box_height = parse_crop(box)
        if max(xs) - min(xs) > box_width / 2 or max(ys) - min(ys) > box_height / 2:
            notes.append("the camera moves during this clip, so one crop cannot follow it: check the render")
    else:
        far = [crop for crop in (find_clip_crop(pick, second, box, FAR_SEARCH_MARGIN) for second in seconds) if crop]
        if not far:
            return {"problems": ["no face found in this clip: set the crop by hand"], "notes": []}
        crop = far[len(far) // 2]
        notes.append("a face was found far from the usual camera spot: check that it is the streamer")
    return {"problems": clip_store.update_clip(name, {"cam_crop": crop, "cam_unverified": bool(notes)}), "notes": notes}


def thumbnail(name: str):
    clip = next((clip for clip in clip_store.load_clips() if clip["name"] == name), None)
    if clip is None:
        return None
    video = CLIPS_DIR / clip["streamer"] / f"{name}.mp4"
    if not video.is_file():
        return None
    thumb = THUMBS_DIR / f"{name}.jpg"
    with thumb_lock(name):
        # one request at a time per clip: a second one finds the picture made, and nobody reads it while it is replaced
        with THUMB_JOBS:
            if not thumb.is_file() or thumb.stat().st_mtime < video.stat().st_mtime:
                if not make_thumbnail(video, thumb):
                    return None
        return thumb.read_bytes()


def thumb_lock(name: str):
    with THUMB_LOCKS_GUARD:
        # shortcut: one small lock per clip name is kept for the life of the server; fine for hundreds of clips
        return THUMB_LOCKS.setdefault(name, threading.Lock())


def make_thumbnail(video: Path, thumb: Path):
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    # one temporary name per request, so two requests for the same clip never write the same file
    temporary = thumb.with_name(f"{thumb.stem}.{threading.get_ident()}.tmp.jpg")
    temporary.unlink(missing_ok=True)
    command = ["ffmpeg", "-v", "error", "-y", "-ss", "1", "-i", str(video), "-frames:v", "1",
               "-vf", f"scale={THUMB_WIDTH}:-2", "-q:v", "4", str(temporary)]
    try:
        # a hung ffmpeg must not keep its slot forever
        result = subprocess.run(command, capture_output=True, timeout=THUMB_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        temporary.unlink(missing_ok=True)
        return False
    if result.returncode != 0 or not temporary.is_file():
        temporary.unlink(missing_ok=True)
        return False
    os.replace(temporary, thumb)
    return True


def start_job(key: str, work) -> bool:
    with JOBS_LOCK:
        if JOBS.get(key) == "running":
            return False
        JOBS[key] = "running"
    threading.Thread(target=run_job, args=(key, work), daemon=True).start()
    return True


def job_failure_reason(error: Exception) -> str:
    if isinstance(error, subprocess.TimeoutExpired):
        return f"{Path(str(error.cmd[0])).name} timed out after {int(error.timeout)} s"
    if isinstance(error, subprocess.CalledProcessError):
        # the command line can be long, and the tool's own message is in the server window
        return f"{Path(str(error.cmd[0])).name} failed with code {error.returncode}; see the server window"
    return str(error)[:MAX_REASON_LENGTH]


def run_job(key: str, work) -> None:
    try:
        work()
        JOBS[key] = "done"
    except Exception as error:
        JOBS[key] = f"failed: {job_failure_reason(error)}"
        logger.error(f"job '{key}' failed: {error}")


def find_source(source_id):
    return next((source for source in load_sources(SOURCES_ROOT) if source["id"] == source_id), None)


def find_candidate(source: dict, candidate_id):
    return next((candidate for candidate in load_candidates(source["id"], SOURCES_ROOT) if candidate["id"] == candidate_id), None)


def window_audio(source_id: str, candidate_id: str):
    source = find_source(source_id)
    candidate = find_candidate(source, candidate_id) if source else None
    if candidate is None:
        return None
    wav = SOURCES_ROOT / source["id"] / "win" / f"w{candidate['start']}.wav"
    return wav.read_bytes() if wav.is_file() else None


def cut_clip_name(clips: list, source_id: str, candidate_id: str):
    # a pick is named <source>_<candidate>_<from>_<to>, so every clip cut from this moment starts with the same prefix
    prefix = f"{source_id}_{candidate_id}_"
    return next((clip["name"] for clip in clips if str(clip.get("pick", "")).startswith(prefix)), None)


def sources_state() -> list:
    # the clip already cut from a moment is found by its pick name, so the page can show it after a reload or restart
    clips = clip_store.load_clips()
    return [dict(source, job=JOBS.get(f"scan:{source['id']}", ""), candidates=[
        dict(candidate, clip=cut_clip_name(clips, source["id"], candidate["id"]))
        for candidate in load_candidates(source["id"], SOURCES_ROOT)])
            for source in load_sources(SOURCES_ROOT)]


def add_source_route(body: dict):
    if body.get("creator") not in clip_store.load_presets().get("creators", {}):
        return 400, {"problems": ["creator: pick one from the Creators list"]}
    try:
        return 200, {"source": add_source(body.get("url"), body["creator"], root=SOURCES_ROOT)}
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError) as error:
        logger.warning(f"could not read video info: {error!r}")
        return 400, {"problems": ["could not read that video; check the link and that it is public"]}
    except ValueError as error:
        return 400, {"problems": [str(error)]}


def start_scan(source_id):
    if find_source(source_id) is None:
        return 400, {"problems": ["no such source"]}
    if not start_job(f"scan:{source_id}", lambda: scan_source(source_id, root=SOURCES_ROOT)):
        return 409, {"problems": ["this source is already being scanned"]}
    return 200, {"started": source_id}


def start_approval(body: dict):
    source = find_source(body.get("source"))
    candidate = find_candidate(source, body.get("candidate")) if source else None
    if candidate is None:
        return 400, {"problems": ["no such source or candidate"]}
    approval = {key: body[key] for key in ("name", "title", "hook", "body", "from", "to") if key in body}
    problems = check_approval(approval, candidate)
    if any(clip["name"] == approval.get("name") for clip in clip_store.load_clips()):
        problems.append("name: a clip with this name already exists")
    if problems:
        return 400, {"problems": problems}

    def work():
        RENDERS[approval["name"]] = "running"
        try:
            approve_candidate(source, candidate, approval)
        except BaseException:
            RENDERS.pop(approval["name"], None)
            raise
        RENDERS[approval["name"]] = "done"

    if not start_job(f"approve:{source['id']}:{candidate['id']}", work):
        return 409, {"problems": ["this moment is already being cut"]}
    return 200, {"started": approval["name"]}


def autoqueue_route(name):
    try:
        return 200, {"slots": queue_clip(name)}
    except ValueError as error:
        return 400, {"problems": [str(error)]}


def instagram_entries() -> list:
    # read only: what the page may show of a queue entry (never the upload address or container id, which belong to Meta)
    path = instagram_schedule.DEFAULT_QUEUE
    try:
        queue = json.loads(path.read_text(encoding="utf8")) if path.exists() else []
    except (OSError, ValueError):
        # shortcut: an unreadable queue file shows as empty so the page still loads; the watcher's log names the real error
        return []
    if not isinstance(queue, list):
        return []
    return [{key: entry[key] for key in INSTAGRAM_SHOWN if key in entry} for entry in queue if isinstance(entry, dict)]


def render_progress_by_name():
    clips = {clip["name"]: clip for clip in clip_store.load_clips()}
    return {name: render_progress(clips[name]) for name, outcome in list(RENDERS.items()) if outcome == "running" and name in clips}


def build_state():
    queue = queue_store.load_queue()
    facebook = facebook_queue.load_queue()
    return {"clips": clip_store.load_clips(), "presets": clip_store.load_presets(), "queue": queue,
            "slots": queue_store.suggest_slots(queue, SLOTS_SHOWN), "daily_limit": queue_store.DAILY_UPLOAD_LIMIT,
            "facebook_queue": facebook, "facebook_slots": queue_store.suggest_slots(facebook, SLOTS_SHOWN),
            "facebook_daily_limit": facebook_queue.DAILY_LIMIT, "facebook_ready": facebook_schedule.CREDENTIALS_PATH.exists(),
            "facebook_login_expired": any(facebook_schedule.TOKEN_EXPIRED_MESSAGE in line for line in log_tail(facebook_schedule.LOG_PATH)),
            "instagram_queue": instagram_entries(), "instagram_slots": queue_store.suggest_slots([e for e in instagram_entries() if "publish_at" in e], SLOTS_SHOWN),
            "renders": RENDERS, "render_progress": render_progress_by_name(), "upload": {"running": upload_running()},
            "login_expired": any(LOGIN_EXPIRED_TEXT in line for line in log_tail()),
            "picks": sorted(path.stem for path in PICKS.glob("*.mp4")),
            "rendered": sorted(path.as_posix() for path in CLIPS_DIR.glob("*/*.mp4")),
            "sources": sources_state(), "jobs": dict(JOBS)}


class ClipServer(ThreadingHTTPServer):
    # the default queue of 5 waiting connections is too short for a page that loads many pictures
    request_queue_size = 128
    daemon_threads = True


def make_handler(token: str):
    class Handler(BaseHTTPRequestHandler):
        def allowed(self):
            port = self.server.server_address[1]
            # a web page on another site could aim the browser at this server; the Host check blocks DNS rebinding
            if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                return False
            given = self.headers.get("X-Token") or parse_qs(urlparse(self.path).query).get("token", [""])[0]
            return secrets.compare_digest(given, token)

        def reply(self, status: int, body, content_type: str = "application/json", headers: dict = None):
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def read_body(self):
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

        def send_video(self, name: str):
            clip = next((c for c in clip_store.load_clips() if c["name"] == name), None)
            path = CLIPS_DIR / clip["streamer"] / f"{name}.mp4" if clip else None
            if path is None or not path.is_file():
                return self.reply(404, {"problems": ["no video"]})
            size = path.stat().st_size
            span = video_range(self.headers.get("Range"), size)
            if span is None:
                return self.reply(416, b"", "video/mp4", {"Content-Range": f"bytes */{size}"})
            with path.open("rb") as video:
                video.seek(span[0])
                data = video.read(span[1] - span[0] + 1)
            self.reply(206, data, "video/mp4", {"Content-Range": f"bytes {span[0]}-{span[1]}/{size}", "Accept-Ranges": "bytes"})

        def do_GET(self):
            if not self.allowed():
                return self.reply(403, {"problems": ["forbidden"]})
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if url.path == "/" and PAGE_PATH.exists():
                return self.reply(200, PAGE_PATH.read_bytes(), "text/html; charset=utf-8")
            if url.path == "/api/state":
                return self.reply(200, build_state())
            if url.path == "/api/log":
                return self.reply(200, {"lines": log_tail()})
            if url.path == "/api/facebook/log":
                return self.reply(200, {"lines": log_tail(facebook_schedule.LOG_PATH)})
            if url.path == "/api/text":
                clip = next((c for c in clip_store.load_clips() if c["name"] == query.get("clip", [""])[0]), None)
                if clip is None:
                    return self.reply(400, {"problems": ["clip: no such clip"]})
                try:
                    return self.reply(200, clip_store.build_text(clip, query.get("platform", [""])[0],
                                                                 clip_store.load_presets()))
                except ValueError as error:
                    return self.reply(400, {"problems": [str(error)]})
            if url.path == "/api/captions":
                clip = next((c for c in clip_store.load_clips() if c["name"] == query.get("clip", [""])[0]), None)
                if clip is None:
                    return self.reply(400, {"problems": ["clip: no such clip"]})
                return self.reply(200, {"captions": [{"start": round(start, 2), "end": round(end, 2), "text": text, "key": key}
                                                      for start, end, text, key in clip_captions(clip)]})
            if url.path == "/api/video":
                return self.send_video(query.get("clip", [""])[0])
            if url.path == "/api/thumb":
                picture = thumbnail(query.get("clip", [""])[0])
                if picture is None:
                    return self.reply(404, {"problems": ["no picture"]})
                return self.reply(200, picture, "image/jpeg")
            if url.path == "/api/frame":
                try:
                    seconds = float(query.get("t", ["0"])[0])
                except ValueError:
                    return self.reply(400, {"problems": ["t: a number of seconds"]})
                if not math.isfinite(seconds) or seconds < 0:
                    return self.reply(400, {"problems": ["t: a number of seconds"]})
                png = frame_png(query.get("pick", [""])[0], seconds)
                return self.reply(200, png, "image/png") if png else self.reply(404, {"problems": ["no such pick"]})
            if url.path == "/api/window":
                audio = window_audio(query.get("source", [""])[0], query.get("candidate", [""])[0])
                return self.reply(200, audio, "audio/wav") if audio else self.reply(404, {"problems": ["no such audio"]})
            self.reply(404, {"problems": ["not found"]})

        def do_POST(self):
            if not self.allowed():
                return self.reply(403, {"problems": ["forbidden"]})
            path = urlparse(self.path).path
            try:
                body = self.read_body()
            except ValueError:
                return self.reply(400, {"problems": ["body is not JSON"]})
            if not isinstance(body, dict):
                return self.reply(400, {"problems": ["body must be a JSON object"]})
            try:
                status, answer = self.run_post(path, body)
            except (TypeError, ValueError, AttributeError, KeyError) as error:
                logger.warning(f"bad request to '{path}': {error!r}")
                status, answer = 400, {"problems": ["the request has wrong or missing fields"]}
            self.reply(status, answer)

        def run_post(self, path: str, body: dict):
            if path == "/api/render":
                return start_render(body.get("name", ""))
            if path == "/api/source":
                return add_source_route(body)
            if path == "/api/source/scan":
                return start_scan(body.get("id"))
            if path in ("/api/candidate/approve", "/api/clip/autoqueue"):
                # both change clips.json or the queues, which the uploader rewrites while it runs
                if upload_running():
                    return 409, {"problems": [UPLOAD_RUNNING_PROBLEM]}
                return start_approval(body) if path == "/api/candidate/approve" else autoqueue_route(body.get("name"))
            # looked up here, not at import, so each call uses the current functions
            starters = {"/api/upload": start_upload, "/api/facebook/upload": start_facebook_upload}
            if path in starters:
                with UPLOAD_LOCK:
                    if upload_running():
                        return 409, {"problems": ["an upload is already running"]}
                    UPLOAD["process"] = starters[path](bool(body.get("dry_run", True)))
                return 200, {"started": True}
            if path not in ("/api/clip", "/api/clip/update", "/api/clip/camera", "/api/queue", "/api/facebook/queue", "/api/instagram/queue", "/api/preset",
                        "/api/creator"):
                return 404, {"problems": ["not found"]}
            # the uploader rewrites the queue file after each upload; changing files under it would lose edits
            if upload_running():
                return 409, {"problems": [UPLOAD_RUNNING_PROBLEM]}
            extra = {}
            if path == "/api/clip":
                problems = clip_store.add_clip(body)
            elif path == "/api/clip/update":
                problems = clip_store.update_clip(body.pop("name", ""), body)
            elif path == "/api/clip/camera":
                extra = find_camera(body.get("name", ""))
                problems = extra.pop("problems")
            elif path == "/api/creator":
                problems = clip_store.save_creator(body.get("name"), body.get("creator"))
            elif path == "/api/preset":
                problems = clip_store.save_preset(body.get("kind"), body.get("name"), body.get("preset"))
            elif path == "/api/instagram/queue":
                problems = queue_file_problems(body) or instagram_queue.add_entry(body)
            elif path == "/api/facebook/queue":
                problems = queue_file_problems(body) or facebook_queue.add_entry(body)
            else:
                problems = queue_file_problems(body) or queue_store.add_entry(body)
            return (400 if problems else 200), dict({"problems": problems}, **extra)

        def log_message(self, format, *args):
            logger.info(f"{self.command} {self.path.split('?')[0]} -> {args[1] if len(args) > 1 else ''}")

    return Handler


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    token = secrets.token_urlsafe(16)
    server = ClipServer(("127.0.0.1", 0), make_handler(token))
    link = f"http://127.0.0.1:{server.server_address[1]}/?token={token}"
    logger.info(f"clip page at {link}")
    webbrowser.open(link)
    server.serve_forever()


if __name__ == "__main__":
    main()
