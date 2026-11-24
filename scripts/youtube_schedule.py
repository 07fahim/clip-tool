"""Upload clips to YouTube as private with a scheduled publish time (YouTube makes them public at that time).
Usage (youtube env): D:/Tools/youtube-env/Scripts/python scripts/youtube_schedule.py [--queue FILE] [--dry-run]
Queue = JSON list of {file, title, description, publish_at}; the video_id is written back after each upload.
"""
import argparse
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# secrets stay outside the project folder
CLIENT_SECRET_PATH = Path("D:/Tools/youtube-env/client_secret.json")
TOKEN_PATH = Path("D:/Tools/youtube-env/token.json")
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
DEFAULT_QUEUE = Path("clips/youtube_queue.json")
LOG_PATH = Path("logs/youtube_schedule.log")
# YouTube rejects titles over 100 characters
MAX_TITLE_LENGTH = 100
GAMING_CATEGORY_ID = "20"

logger = logging.getLogger("youtube_schedule")


def check_entry(entry: dict):
    problems = []
    if not Path(entry.get("file", "")).is_file():
        problems.append(f"file not found: {entry.get('file')}")
    if not entry.get("title") or len(entry["title"]) > MAX_TITLE_LENGTH:
        problems.append(f"title empty or over {MAX_TITLE_LENGTH} characters")
    try:
        publish_at = datetime.fromisoformat(entry.get("publish_at", ""))
        if publish_at.tzinfo is None:
            problems.append("publish_at needs a timezone, for example 2026-11-23T07:30:00+06:00")
        elif publish_at <= datetime.now(timezone.utc):
            problems.append("publish_at is in the past")
    except (ValueError, TypeError):
        problems.append("publish_at is not an ISO time")
    return problems


def authorize():
    credentials = None
    if TOKEN_PATH.exists():
        credentials = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except RefreshError:
            # an app in Testing status loses its refresh token after 7 days; log in again
            logger.warning("login expired, opening the Google login")
            credentials = None
    if not credentials or not credentials.valid:
        credentials = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES).run_local_server(port=0)
    TOKEN_PATH.write_text(credentials.to_json(), encoding="utf8")
    return build("youtube", "v3", credentials=credentials)


def upload(youtube, entry: dict):
    body = {
        "snippet": {"title": entry["title"], "description": entry["description"], "categoryId": GAMING_CATEGORY_ID},
        # publishAt only works on a private video
        "status": {"privacyStatus": "private", "publishAt": entry["publish_at"], "selfDeclaredMadeForKids": False},
    }
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=MediaFileUpload(entry["file"], resumable=True))
    response = None
    while response is None:
        _, response = request.next_chunk()
    return response["id"]


def save_queue(queue_path: Path, queue: list):
    # write beside the file and swap, so a crash mid-write cannot leave half a queue
    temporary = queue_path.with_name(queue_path.name + ".tmp")
    temporary.write_text(json.dumps(queue, indent=2, ensure_ascii=False), encoding="utf8")
    os.replace(temporary, queue_path)


def run(queue_path: Path, dry_run: bool):
    queue = json.loads(queue_path.read_text(encoding="utf8"))
    logger.info(f"scheduling [{len(queue)}] clips from '{queue_path}' (dry run: {dry_run})")
    youtube = None if dry_run else authorize()
    done = skipped = failed = 0
    for index, entry in enumerate(queue, 1):
        if entry.get("video_id"):
            skipped += 1
            logger.info(f"[{index}/{len(queue)}] '{entry['title']}': already uploaded as {entry['video_id']}")
            continue
        problems = check_entry(entry)
        if problems:
            failed += 1
            logger.error(f"[{index}/{len(queue)}] '{entry.get('title')}': {'; '.join(problems)}")
            continue
        if dry_run:
            done += 1
            logger.info(f"[{index}/{len(queue)}] '{entry['title']}': would upload for {entry['publish_at']}")
            continue
        try:
            entry["video_id"] = upload(youtube, entry)
        except Exception as error:
            failed += 1
            logger.error(f"[{index}/{len(queue)}] '{entry['title']}': upload failed: {error}")
            continue
        save_queue(queue_path, queue)
        done += 1
        logger.info(f"[{index}/{len(queue)}] '{entry['title']}': uploaded {entry['video_id']} for {entry['publish_at']}")
    logger.info(f"finished: {'would upload' if dry_run else 'uploaded'} [{done}], skipped [{skipped}], failed [{failed}]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE, help="JSON list of clips to schedule")
    parser.add_argument("--dry-run", action="store_true", help="check the queue and print the plan, upload nothing")
    arguments = parser.parse_args()
    LOG_PATH.parent.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.FileHandler(LOG_PATH, encoding="utf8"), logging.StreamHandler()])
    run(arguments.queue, arguments.dry_run)
