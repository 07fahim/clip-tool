"""Publish clips to Instagram as reels at the set time. Instagram's API cannot hold a scheduled post (Facebook's can),
so this tool must be running (--watch) when a reel is due; Meta does not publish anything while the PC is off.
Usage (youtube env): D:/Tools/youtube-env/Scripts/python scripts/instagram_schedule.py
    [--queue FILE] [--dry-run] [--watch] [--check-token]
Queue = JSON list of {file, caption, publish_at}. container_id is written back as soon as Meta accepts the upload start,
status "published" (with media_id) after the last call. An entry that already has a container_id never gets a second
container.
Token file D:/Tools/meta/instagram.json holds {ig_user_id, access_token}; the token is never logged."""

import argparse
import errno
import json
import logging
import msvcrt
import time
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from clip_store import CLIPS_DIR
from facebook_schedule import GRAPH_URL, ApiError, AuthError, scrub
from facebook_schedule import bearer as meta_bearer
from facebook_schedule import read_answer as meta_read_answer
from youtube_schedule import save_queue

# secrets stay outside the project folder
CREDENTIALS_PATH = Path("D:/Tools/meta/instagram.json")
DEFAULT_QUEUE = Path("clips/instagram_queue.json")
LOG_PATH = Path("logs/instagram_schedule.log")
# a reel more than this late (PC was off) is not posted: a stale clip is worse than a missed slot
MAX_LATE_HOURS = 6
# Meta processes an uploaded reel for a while; one check every 5 seconds for up to 5 minutes
POLL_SECONDS = 5
MAX_POLLS = 60
# a failing reel is tried this many times, then left as needs_check; the wait before try n+1 is n times RETRY_MINUTES,
# so four tries cover 15 + 30 + 45 minutes of Meta trouble instead of three minutes
MAX_ATTEMPTS = 4
# Meta's throttling codes (4 app, 17 user, 32 page, 613 custom, 80002 Instagram limit): only waiting helps, and the
# reel is not at fault. Only 4 was seen live (2026-11-24); the others are from Meta's documentation
RATE_LIMIT_CODES = (4, 17, 32, 613, 80002)
RATE_LIMIT_WAIT_MINUTES = 45
RETRY_MINUTES = 15
# how often --watch looks for reels that are due
WATCH_SECONDS = 60
# the token is only ever sent to Meta's own upload host, never to an address read from the queue file
META_UPLOAD_PREFIX = "https://rupload.facebook.com/"
SKIPPED_STATUSES = ("published", "missed", "needs_check")
TOKEN_EXPIRED_MESSAGE = "token expired, make a new system-user token"
PLACEHOLDER_PREFIX = "PASTE_"

logger = logging.getLogger("instagram_schedule")


class RateLimited(ApiError):
    pass


class CheckNeeded(ApiError):
    # a container Meta says is dead: trying again cannot help, the owner decides
    pass


def check_entry(entry: dict, clips_dir: Path = CLIPS_DIR):
    problems = []
    file_name = entry.get("file")
    if not isinstance(file_name, str) or not file_name.endswith(".mp4") or not Path(file_name).is_file():
        problems.append(f"file: not found or not an .mp4: {file_name}")
    elif clips_dir.resolve() not in Path(file_name).resolve().parents:
        problems.append("file: must be inside the clips folder")
    if not isinstance(entry.get("caption"), str):
        problems.append("caption: must be text")
    try:
        if datetime.fromisoformat(entry.get("publish_at", "")).tzinfo is None:
            problems.append("publish_at needs a timezone, for example 2026-11-26T18:00:00+06:00")
    except (ValueError, TypeError):
        problems.append("publish_at is not an ISO time")
    return problems


def load_credentials(path: Path = CREDENTIALS_PATH):
    if not path.exists():
        raise SystemExit(f"no token file at {path}; do the token steps first")
    credentials = json.loads(path.read_text(encoding="utf-8-sig"))
    for key in ("ig_user_id", "access_token"):
        if not credentials.get(key) or str(credentials[key]).startswith(PLACEHOLDER_PREFIX):
            raise SystemExit(f"{path} has no {key}; do the token steps first")
    return credentials


def read_answer(response, credentials: dict):
    try:
        return meta_read_answer(response, credentials, "access_token")
    except ApiError as error:
        if any(str(error).startswith(f"Meta error {code}:") for code in RATE_LIMIT_CODES):
            raise RateLimited(str(error)) from error
        raise


def bearer(credentials: dict):
    return meta_bearer(credentials, "access_token")


def find_stored(queue: list, entry: dict):
    for stored in queue:
        if (
            isinstance(stored, dict)
            and stored.get("file") == entry["file"]
            and stored.get("publish_at") == entry["publish_at"]
        ):
            return stored
    return None


def save_entry(queue_path: Path, entry: dict):
    # re-read the file first: a hand edit made while a reel was uploading must not be overwritten
    queue = json.loads(queue_path.read_text(encoding="utf8"))
    stored = find_stored(queue, entry)
    if stored is None:
        raise LookupError("the entry was edited or removed in the queue file during the pass")
    stored.clear()
    stored.update(entry)
    save_queue(queue_path, queue)


def save_safely(queue_path: Path, entry: dict, label: str):
    try:
        save_entry(queue_path, entry)
    except (OSError, ValueError, LookupError) as error:
        logger.error(f"{label}: queue was not saved ({error}); the entry is '{entry.get('status')}' in memory only")


def lock_path(queue_path: Path):
    return queue_path.with_name(queue_path.name + ".lock")


@contextmanager
def queue_lock(queue_path: Path):
    # one pass at a time: two passes would both see a due reel without a container and publish it twice.
    # The operating system holds the lock while this process lives and drops it when the process dies, so a crashed
    # pass can never leave a stuck lock behind. shortcut: msvcrt is Windows only; the project runs on Windows, move to
    # fcntl if it ever does not
    path = lock_path(queue_path)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"x")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            # a lock held by another pass answers EACCES; anything else is a real fault, not contention
            if error.errno != errno.EACCES:
                raise
            yield False
            return
        try:
            yield True
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    # the file itself is only a name for the lock; removing it is tidiness and may fail while another pass has it open
    with suppress(OSError):
        path.unlink()


def start_upload(session, credentials: dict, entry: dict):
    return read_answer(
        session.post(
            f"{GRAPH_URL}/{credentials['ig_user_id']}/media",
            headers=bearer(credentials),
            timeout=60,
            data={"media_type": "REELS", "upload_type": "resumable", "caption": entry["caption"]},
        ),
        credentials,
    )


def container_status(session, credentials: dict, container_id: str):
    answer = read_answer(
        session.get(
            f"{GRAPH_URL}/{container_id}", params={"fields": "status_code"}, headers=bearer(credentials), timeout=30
        ),
        credentials,
    )
    return answer.get("status_code")


def send_video(session, credentials: dict, entry: dict, upload_url: str):
    if not upload_url.startswith(META_UPLOAD_PREFIX):
        raise ApiError("the saved upload address is not Meta's; the token was not sent")
    file_path = Path(entry["file"])
    with file_path.open("rb") as video:
        headers = {
            "Authorization": f"OAuth {credentials['access_token']}",
            "offset": "0",
            "file_size": str(file_path.stat().st_size),
        }
        answer = read_answer(session.post(upload_url, data=video, headers=headers, timeout=600), credentials)
    # Meta answers {"success": true} when the bytes arrived; a live upload once got an answer without it and nothing
    # was stored
    if not (isinstance(answer, dict) and answer.get("success") is True):
        # the answer is often a false alarm: five times on 2026-11-24 Meta showed the container FINISHED after it
        if container_status(session, credentials, entry["container_id"]) in ("FINISHED", "PUBLISHED"):
            logger.warning(f"upload unconfirmed but the container is finished, taken as uploaded: {str(answer)[:80]}")
            return
        raise ApiError(f"Meta did not confirm the upload: {str(answer)[:200]}")


def wait_until_finished(session, credentials: dict, container_id: str, pause):
    for _ in range(MAX_POLLS):
        answer = read_answer(
            session.get(
                f"{GRAPH_URL}/{container_id}", params={"fields": "status_code"}, headers=bearer(credentials), timeout=30
            ),
            credentials,
        )
        if answer.get("status_code") in ("FINISHED", "PUBLISHED"):
            return answer["status_code"]
        if answer.get("status_code") in ("ERROR", "EXPIRED"):
            raise CheckNeeded(f"Meta says the video is {answer['status_code']}")
        pause(POLL_SECONDS)
    raise ApiError("the video was still processing after the wait")


def publish(session, credentials: dict, entry: dict, save, pause):
    if not entry.get("container_id"):
        started = start_upload(session, credentials, entry)
        # saved at once: a crash after this point must never lead to a second upload of the same clip
        entry["container_id"] = started["id"]
        entry["upload_url"] = started.get("uri")
        save()
        if not entry["upload_url"]:
            raise ApiError("Meta gave no upload address")
    # the address is kept so a crash during the upload is finished on the next pass, without a second container
    if entry.get("upload_url") and not entry.get("uploaded"):
        send_video(session, credentials, entry, entry["upload_url"])
        entry["uploaded"] = True
        save()
    if wait_until_finished(session, credentials, entry["container_id"], pause) == "PUBLISHED":
        # the publish call timed out or its answer was lost, but Meta did publish it
        entry["media_id"] = "unknown"
        entry["status"] = "published"
        logger.warning(f"'{entry['caption'][:30]}' was already published by Meta; media_id unknown, check the account")
        return
    answer = read_answer(
        session.post(
            f"{GRAPH_URL}/{credentials['ig_user_id']}/media_publish",
            headers=bearer(credentials),
            timeout=60,
            data={"creation_id": entry["container_id"]},
        ),
        credentials,
    )
    entry["media_id"] = answer["id"]
    entry["status"] = "published"


def record_failure(queue_path: Path, entry: dict, error: Exception, credentials: dict, label: str, now: datetime):
    message = scrub(error, credentials["access_token"])
    if isinstance(error, (requests.ConnectionError, requests.Timeout)):
        # a dropped connection says nothing about the reel: the next pass tries again without using up an attempt
        logger.warning(f"{label}: network problem, will try again: {message}")
        return
    if isinstance(error, RateLimited):
        entry["error"] = message
        entry["retry_after"] = (now + timedelta(minutes=RATE_LIMIT_WAIT_MINUTES)).isoformat(timespec="seconds")
        logger.warning(f"{label}: Meta's request limit, will try again after {entry['retry_after']}: {message}")
        save_safely(queue_path, entry, label)
        return
    entry["attempts"] = entry.get("attempts", 0) + 1
    entry["error"] = message
    if isinstance(error, CheckNeeded) or entry["attempts"] >= MAX_ATTEMPTS:
        entry["status"] = "needs_check"
        logger.error(
            f"{label}: needs a check: {message}. To try again delete status, attempts, error and retry_after "
            f"from this entry in "
            f"'{queue_path}' (also container_id, upload_url and uploaded to start with a new container)"
        )
    else:
        entry["retry_after"] = (now + timedelta(minutes=RETRY_MINUTES * entry["attempts"])).isoformat(
            timespec="seconds"
        )
        reuse = "; the same container is used next time" if entry.get("container_id") else ""
        logger.error(
            f"{label}: failed [{entry['attempts']}/{MAX_ATTEMPTS}]: {message}{reuse}; "
            f"next try after {entry['retry_after']}"
        )
    save_safely(queue_path, entry, label)


def run(
    queue_path: Path,
    dry_run: bool,
    session=None,
    now=None,
    clips_dir: Path = CLIPS_DIR,
    credentials: dict | None = None,
    pause=time.sleep,
):
    if dry_run:
        return go_through_queue(queue_path, dry_run, session, now, clips_dir, credentials, pause)
    with queue_lock(queue_path) as got_lock:
        if not got_lock:
            logger.warning(f"another pass is running (it holds '{lock_path(queue_path)}'); this pass is skipped")
            return True
        return go_through_queue(queue_path, dry_run, session, now, clips_dir, credentials, pause)


def entry_label(index: int, total: int, entry):
    caption = entry.get("caption") if isinstance(entry, dict) else entry
    return f"[{index}/{total}] '{str(caption)[:30]}'"


def make_clock(now):
    # now is None for the real clock, a datetime to freeze it (tests), or a function that is called at each reading
    if now is None:
        return lambda: datetime.now(timezone.utc)
    return now if callable(now) else lambda: now


def is_waiting(entry: dict, now: datetime, label: str):
    value = entry.get("retry_after")
    if not value:
        return False
    try:
        retry_at = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        retry_at = None
    if retry_at is None or retry_at.tzinfo is None:
        # a hand-edited value must not stop the reel, or the ones behind it
        logger.warning(f"{label}: retry_after {value!r} is not a time with a timezone; ignored")
        return False
    return retry_at > now


def handle_entry(
    queue_path: Path,
    entry: dict,
    label: str,
    clock,
    dry_run: bool,
    session,
    credentials: dict | None,
    clips_dir: Path,
    pause,
):
    # returns what happened to this one entry: done, waiting, skipped or failed
    now = clock()
    if entry.get("status") in SKIPPED_STATUSES:
        if entry["status"] == "needs_check":
            logger.warning(
                f"{label}: needs_check ({entry.get('error')}); "
                "delete status, attempts, error and retry_after to try again"
            )
        return "skipped"
    problems = check_entry(entry, clips_dir)
    if problems:
        logger.error(f"{label}: {'; '.join(problems)}")
        return "failed"
    publish_at = datetime.fromisoformat(entry["publish_at"])
    if publish_at > now:
        return "waiting"
    # the file is read again for each due reel: an earlier reel took minutes and the owner may have edited the file
    # since
    stored = find_stored(json.loads(queue_path.read_text(encoding="utf8")), entry)
    if stored is None:
        logger.warning(f"{label}: edited or removed in the queue file during the pass; left for the next pass")
        return "skipped"
    entry.clear()
    entry.update(stored)
    if entry.get("status") in SKIPPED_STATUSES:
        return "skipped"
    if is_waiting(entry, now, label):
        return "waiting"
    if not entry.get("container_id") and now - publish_at > timedelta(hours=MAX_LATE_HOURS):
        entry["status"] = "missed"
        if not dry_run:
            save_safely(queue_path, entry, label)
        logger.warning(
            f"{label}: missed, was due {entry['publish_at']} and is more than {MAX_LATE_HOURS} hours late; not posted"
        )
        return "skipped"
    if dry_run:
        logger.info(f"{label}: would publish now (due {entry['publish_at']})")
        return "done"
    try:
        publish(session, credentials, entry, lambda: save_entry(queue_path, entry), pause)
    except AuthError:
        raise
    except Exception as error:
        # read again: the upload and the wait took minutes since the entry started
        record_failure(queue_path, entry, error, credentials, label, clock())
        return "failed"
    entry.pop("error", None)
    entry.pop("retry_after", None)
    save_safely(queue_path, entry, label)
    logger.info(f"{label}: published as {entry['media_id']}")
    return "done"


def go_through_queue(
    queue_path: Path, dry_run: bool, session, now: datetime | None, clips_dir: Path, credentials: dict | None, pause
):
    clock = make_clock(now)
    queue = json.loads(queue_path.read_text(encoding="utf8"))
    logger.info(f"checking [{len(queue)}] reels in '{queue_path}' (dry run: {dry_run})")
    if not dry_run:
        credentials = credentials or load_credentials()
        session = session or requests.Session()
    counts = dict.fromkeys(("done", "waiting", "skipped", "failed"), 0)
    token_ok = True
    seen = set()
    for index, entry in enumerate(queue, 1):
        label = entry_label(index, len(queue), entry)
        # repr: a hand-edited value may be a list, which cannot go into a set
        key = (repr(entry.get("file")), repr(entry.get("publish_at"))) if isinstance(entry, dict) else None
        if key is not None and key in seen:
            # entries are found again in the file by file + publish_at, so a second one with the same pair cannot be
            # told apart
            counts["failed"] += 1
            logger.error(
                f"{label}: duplicate of an earlier entry (same file and publish_at); not published, change one of them"
            )
            continue
        seen.add(key)
        try:
            outcome = handle_entry(queue_path, entry, label, clock, dry_run, session, credentials, clips_dir, pause)
        except AuthError:
            logger.error(TOKEN_EXPIRED_MESSAGE)
            token_ok = False
            break
        except Exception as error:
            # one broken entry must not stop the ones behind it
            outcome = "failed"
            logger.error(
                f"{label}: unexpected error, left for the next pass: "
                f"{scrub(error, (credentials or {}).get('access_token'))}"
            )
        counts[outcome] += 1
    logger.info(
        f"finished: {'would publish' if dry_run else 'published'} [{counts['done']}], waiting [{counts['waiting']}], "
        f"skipped [{counts['skipped']}], failed [{counts['failed']}]"
    )
    return token_ok


def watch(queue_path: Path, dry_run: bool, pause=time.sleep, **run_arguments):
    while True:
        try:
            token_ok = run(queue_path, dry_run, **run_arguments)
        except (ValueError, OSError) as error:
            # a queue file saved half-way by a hand edit must not end the watch
            logger.error(f"queue file '{queue_path}' not readable: {error}")
            token_ok = True
        except Exception:
            # anything else (a hand-edited queue of the wrong shape, say) is logged and tried again; state is saved at
            # every step
            logger.exception(f"pass failed, trying again in {WATCH_SECONDS} seconds")
            token_ok = True
        if dry_run or not token_ok:
            return
        pause(WATCH_SECONDS)


def check_token():
    credentials = load_credentials()
    session = requests.Session()
    account = read_answer(
        session.get(
            f"{GRAPH_URL}/{credentials['ig_user_id']}",
            params={"fields": "id,username,media_count"},
            headers=bearer(credentials),
            timeout=30,
        ),
        credentials,
    )
    logger.info(
        f"token works for '{account.get('username')}' ({account.get('id')}), posts [{account.get('media_count')}]"
    )
    limit = read_answer(
        session.get(
            f"{GRAPH_URL}/{credentials['ig_user_id']}/content_publishing_limit",
            params={"fields": "quota_usage,config"},
            headers=bearer(credentials),
            timeout=30,
        ),
        credentials,
    )
    logger.info(f"publishing quota: {limit['data'][0]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE, help="JSON list of reels to publish")
    parser.add_argument("--dry-run", action="store_true", help="check the queue and print the plan, publish nothing")
    parser.add_argument(
        "--watch", action="store_true", help="keep running and publish each reel when it is due (Ctrl+C stops)"
    )
    parser.add_argument(
        "--check-token", action="store_true", help="check the saved token and the publishing quota, post nothing"
    )
    arguments = parser.parse_args()
    LOG_PATH.parent.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf8"), logging.StreamHandler()],
    )
    if arguments.check_token:
        check_token()
    elif arguments.watch:
        watch(arguments.queue, arguments.dry_run)
    else:
        run(arguments.queue, arguments.dry_run)
