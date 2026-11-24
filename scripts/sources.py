"""Source videos (a YouTube link per record) kept under footage/sources/<id>/source.json."""
import json
import os
import re
import subprocess
from pathlib import Path

from live_grab import YT_DLP

SOURCES_DIR = Path("footage/sources")
# only plain YouTube watch, live, shorts and youtu.be links with an 11-character video id; the strict match also
# keeps anything that looks like a yt-dlp option or another scheme out of the command
URL_PATTERN = re.compile(
    r"https://(?:www\.)?(?:youtube\.com/(?:watch\?v=|live/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})(?:[?&][A-Za-z0-9_=&.%-]*)?"
)
CLOCK_PATTERN = re.compile(r"(\d+):(\d\d):(\d\d) (.*)")
HEADER_PATTERN = re.compile(r"=== (\d+):(\d\d):(\d\d)")
FETCH_TIMEOUT_SECONDS = 120
# a running live has no end to download, an upcoming one has no video yet and a post_live one is still being processed by YouTube
NOT_READY_STATES = ("is_live", "is_upcoming", "post_live")


def check_url(url) -> list:
    match = URL_PATTERN.fullmatch(url) if isinstance(url, str) else None
    # a second v= in the query would make yt-dlp fetch another video than the id the source is named after
    if match is None or re.search(r"[?&]v=", url[match.end(1):]):
        return ["url: must be a YouTube link such as https://www.youtube.com/watch?v=XXXXXXXXXXX"]
    return []


def source_id_for(url: str) -> str:
    # shortcut: lower-casing a case-sensitive video id could collide in theory; fine for a personal library
    video_id = URL_PATTERN.fullmatch(url).group(1)
    return "v_" + video_id.lower().replace("-", "_")


def fetch_info(url: str) -> dict:
    result = subprocess.run([YT_DLP, "--js-runtimes", "node", "--no-playlist", "--skip-download", "-J", "--", url],
                            capture_output=True, text=True, check=True, timeout=FETCH_TIMEOUT_SECONDS)
    info = json.loads(result.stdout)
    return {"title": info.get("title", ""), "duration": int(info.get("duration") or 0), "live_status": info.get("live_status", "")}


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf8")
    os.replace(temporary, path)


def load_sources(root: Path = SOURCES_DIR) -> list:
    return [json.loads(path.read_text(encoding="utf8")) for path in sorted(root.glob("*/source.json"))]


def save_source(source: dict, root: Path = SOURCES_DIR) -> None:
    write_json(root / source["id"] / "source.json", source)


def add_source(url: str, creator: str, fetch=fetch_info, root: Path = SOURCES_DIR) -> dict:
    problems = check_url(url)
    if problems:
        raise ValueError("; ".join(problems))
    source_id = source_id_for(url)
    existing = root / source_id / "source.json"
    if existing.is_file():
        known = json.loads(existing.read_text(encoding="utf8"))
        if known["creator"] != creator:
            known["creator"] = creator
            save_source(known, root)
        return known
    info = fetch(url)
    if info.get("live_status") in NOT_READY_STATES:
        raise ValueError("this stream is live or not started yet; add it again after it ends and YouTube has processed it")
    source = {"id": source_id, "url": url, "title": info["title"], "duration": info["duration"],
              "creator": creator, "status": "new"}
    save_source(source, root)
    return source


def parse_lines(text: str) -> list:
    # scan_windows.py writes "=== H:MM:SS" per window, then "H:MM:SS words" per spoken line
    windows = []
    for line in text.splitlines():
        if line.startswith("==="):
            header = HEADER_PATTERN.fullmatch(line.strip())
            # a header that cannot be read must not hand its lines to the window before it
            windows.append({"offset": int(header[1]) * 3600 + int(header[2]) * 60 + int(header[3]), "lines": []}
                           if header else {"offset": None, "lines": []})
            continue
        match = CLOCK_PATTERN.fullmatch(line)
        if match and windows:
            hours, minutes, seconds, words = match.groups()
            windows[-1]["lines"].append((int(hours) * 3600 + int(minutes) * 60 + int(seconds), words))
    return [window for window in windows if window["offset"] is not None]
