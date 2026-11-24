"""Upload clips to the Facebook Page as scheduled reels (Meta makes them public at the set time, PC can be off).
Usage (youtube env): D:/Tools/youtube-env/Scripts/python scripts/facebook_schedule.py [--queue FILE] [--dry-run] [--check-token]
Queue = JSON list of {file, caption, publish_at}; reel_id is written back as soon as Meta accepts the upload start,
status "scheduled" after the final call. An entry that already has a reel_id is never uploaded again.
Token file D:/Tools/meta/facebook.json holds {page_id, page_access_token}; the token is never logged."""
import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from clip_store import CLIPS_DIR
from youtube_schedule import save_queue

# secrets stay outside the project folder
CREDENTIALS_PATH = Path("D:/Tools/meta/facebook.json")
DEFAULT_QUEUE = Path("clips/facebook_queue.json")
LOG_PATH = Path("logs/facebook_schedule.log")
# v26.0 is the latest version on Meta's changelog (2026-07-29) and answered every call so far
GRAPH_VERSION = "v26.0"
GRAPH_URL = f"https://graph.facebook.com/{GRAPH_VERSION}"
# Meta accepts a scheduled reel more than 10 minutes and at most 29 days ahead
MIN_LEAD_MINUTES = 10
MAX_LEAD_DAYS = 29
# Meta's page says both "finish" and "complete" for the last call; "finish" is what its example uses
FINISH_PHASE = "finish"
# Graph error codes that mean the token is bad or expired
AUTH_ERROR_CODES = (190, 102)
# warn this many days before the token or its data access runs out
WARN_DAYS = 14
TOKEN_EXPIRED_MESSAGE = "token expired, redo the token steps"
PLACEHOLDER_TOKEN = "PASTE_PAGE_TOKEN_HERE"

logger = logging.getLogger("facebook_schedule")


class AuthError(Exception):
    pass


class ApiError(Exception):
    pass


def scrub(text, token: str):
    return str(text).replace(token, "<token>") if token else str(text)


def check_entry(entry: dict, now: datetime | None = None, clips_dir: Path = CLIPS_DIR):
    now = now or datetime.now(timezone.utc)
    problems = []
    file_name = entry.get("file")
    if not isinstance(file_name, str) or not file_name.endswith(".mp4") or not Path(file_name).is_file():
        problems.append(f"file: not found or not an .mp4: {file_name}")
    elif clips_dir.resolve() not in Path(file_name).resolve().parents:
        problems.append("file: must be inside the clips folder")
    if not isinstance(entry.get("caption"), str):
        problems.append("caption: must be text")
    try:
        publish_at = datetime.fromisoformat(entry.get("publish_at", ""))
        if publish_at.tzinfo is None:
            problems.append("publish_at needs a timezone, for example 2026-11-26T18:00:00+06:00")
        elif publish_at <= now + timedelta(minutes=MIN_LEAD_MINUTES):
            problems.append(f"publish_at must be more than {MIN_LEAD_MINUTES} minutes ahead")
        elif publish_at > now + timedelta(days=MAX_LEAD_DAYS):
            problems.append(f"publish_at must be at most {MAX_LEAD_DAYS} days ahead")
    except (ValueError, TypeError):
        problems.append("publish_at is not an ISO time")
    return problems


def load_credentials(path: Path = CREDENTIALS_PATH):
    if not path.exists():
        raise SystemExit(f"no token file at {path}; do the token steps first")
    credentials = json.loads(path.read_text(encoding="utf-8-sig"))
    if not credentials.get("page_id") or credentials.get("page_access_token") in (None, "", PLACEHOLDER_TOKEN):
        raise SystemExit(f"{path} has no page_access_token; do the token steps first")
    return credentials


def bearer(credentials: dict, key: str = "page_access_token"):
    # the key is a parameter so instagram_schedule.py can reuse this with its own credentials file
    return {"Authorization": f"Bearer {credentials[key]}"}


def read_answer(response, credentials: dict, key: str = "page_access_token"):
    body = response.json()
    if isinstance(body, dict) and "error" in body:
        error = body["error"]
        message = scrub(error.get("message"), credentials[key])
        if error.get("code") in AUTH_ERROR_CODES:
            raise AuthError(message)
        raise ApiError(f"Meta error {error.get('code')}: {message}")
    return body


def token_status(session, credentials: dict, now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    answer = read_answer(session.get(f"{GRAPH_URL}/debug_token", params={"input_token": credentials["page_access_token"]},
                                     headers=bearer(credentials), timeout=30), credentials)["data"]

    def when(key):
        value = answer.get(key)
        return "never" if value == 0 else (datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds") if value else "not given")

    status = {"type": answer.get("type"), "valid": answer.get("is_valid"), "expires_at": when("expires_at"),
              "data_access_expires_at": when("data_access_expires_at"), "scopes": sorted(answer.get("scopes", []))}
    for key, label in (("expires_at", "token"), ("data_access_expires_at", "data access")):
        value = answer.get(key)
        if value and datetime.fromtimestamp(value, timezone.utc) < now + timedelta(days=WARN_DAYS):
            logger.warning(f"{label} ends on {status[key]}; redo the token steps before then")
    return status


def upload(session, credentials: dict, entry: dict, save):
    page_url = f"{GRAPH_URL}/{credentials['page_id']}/video_reels"
    started = read_answer(session.post(page_url, data={"upload_phase": "start"}, headers=bearer(credentials), timeout=60), credentials)
    # saved at once: a crash after this point must never lead to a second upload of the same clip
    entry["reel_id"] = started["video_id"]
    save()
    file_path = Path(entry["file"])
    with file_path.open("rb") as video:
        headers = {"Authorization": f"OAuth {credentials['page_access_token']}", "offset": "0",
                   "file_size": str(file_path.stat().st_size)}
        read_answer(session.post(started["upload_url"], data=video, headers=headers, timeout=600), credentials)
    publish_at = datetime.fromisoformat(entry["publish_at"])
    read_answer(session.post(page_url, headers=bearer(credentials), timeout=60, data={
        "upload_phase": FINISH_PHASE, "video_id": entry["reel_id"], "video_state": "SCHEDULED",
        "scheduled_publish_time": str(int(publish_at.timestamp())), "description": entry["caption"]}), credentials)
    entry["status"] = "scheduled"
    return entry["reel_id"]


def run(queue_path: Path, dry_run: bool, session=None, now: datetime | None = None, clips_dir: Path = CLIPS_DIR,
        credentials: dict | None = None):
    now = now or datetime.now(timezone.utc)
    queue = json.loads(queue_path.read_text(encoding="utf8"))
    logger.info(f"scheduling [{len(queue)}] reels from '{queue_path}' (dry run: {dry_run})")
    done = skipped = failed = 0
    if not dry_run:
        credentials = credentials or load_credentials()
        session = session or requests.Session()
        try:
            token_status(session, credentials, now)
        except AuthError:
            logger.error(TOKEN_EXPIRED_MESSAGE)
            return
    for index, entry in enumerate(queue, 1):
        label = f"[{index}/{len(queue)}] '{str(entry.get('caption'))[:30]}'"
        if entry.get("reel_id"):
            skipped += 1
            if entry.get("status") != "scheduled":
                logger.warning(f"{label}: has reel_id {entry['reel_id']} but is not marked scheduled; needs a check in Business Suite, not uploaded again")
            else:
                logger.info(f"{label}: already scheduled as {entry['reel_id']}")
            continue
        problems = check_entry(entry, now, clips_dir)
        if problems:
            failed += 1
            logger.error(f"{label}: {'; '.join(problems)}")
            continue
        if dry_run:
            done += 1
            logger.info(f"{label}: would upload for {entry['publish_at']}")
            continue
        try:
            upload(session, credentials, entry, lambda: save_queue(queue_path, queue))
        except AuthError:
            logger.error(TOKEN_EXPIRED_MESSAGE)
            break
        except Exception as error:
            failed += 1
            logger.error(f"{label}: upload failed: {scrub(error, credentials['page_access_token'])}")
            continue
        save_queue(queue_path, queue)
        done += 1
        logger.info(f"{label}: scheduled {entry['reel_id']} for {entry['publish_at']}")
    logger.info(f"finished: {'would upload' if dry_run else 'scheduled'} [{done}], skipped [{skipped}], failed [{failed}]")


def check_token():
    credentials = load_credentials()
    session = requests.Session()
    page = read_answer(session.get(f"{GRAPH_URL}/{credentials['page_id']}", params={"fields": "name,id"},
                                   headers=bearer(credentials), timeout=30), credentials)
    logger.info(f"token works for Page '{page.get('name')}' ({page.get('id')})")
    logger.info(f"token status: {token_status(session, credentials)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE, help="JSON list of reels to schedule")
    parser.add_argument("--dry-run", action="store_true", help="check the queue and print the plan, upload nothing")
    parser.add_argument("--check-token", action="store_true", help="check the saved Page token and its dates, post nothing")
    arguments = parser.parse_args()
    LOG_PATH.parent.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.FileHandler(LOG_PATH, encoding="utf8"), logging.StreamHandler()])
    if arguments.check_token:
        check_token()
    else:
        run(arguments.queue, arguments.dry_run)
