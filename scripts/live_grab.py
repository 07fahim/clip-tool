import argparse
import re
import subprocess
import time
import urllib.request
from pathlib import Path

YT_DLP = "D:/Tools/yt-dlp/yt-dlp.exe"
# YouTube live HLS pieces are 2 s long, so stream second t sits in piece number t // 2
SEGMENT_SECONDS = 2
# live: 234 = audio, 312 = 1080p60 video; ended "post_live" stream: 140 = audio, 299 = 1080p60 video
FORMATS = {"audio": "234/140", "video": "312/299"}


def to_seconds(clock: str):
    seconds = 0
    for part in clock.split(":"):
        seconds = seconds * 60 + int(part)
    return seconds


def piece_url_template(video_url: str, format_id: str):
    playlist_url = subprocess.run([YT_DLP, "--js-runtimes", "node", "-g", "-f", format_id, video_url],
                                  capture_output=True, text=True, check=True).stdout.split()[0]
    if ".m3u8" not in playlist_url:
        # ended stream not yet processed: the media URL takes the piece number as &sq=N
        return playlist_url + "&sq={}"
    playlist = urllib.request.urlopen(playlist_url).read().decode()
    piece_url = next(line for line in playlist.splitlines() if "/sq/" in line)
    return re.sub(r"/sq/\d+", "/sq/{}", piece_url)


def fetch_pieces(template: str, first: int, last: int, out_path: Path):
    with open(out_path, "wb") as f:
        for number in range(first, last + 1):
            for attempt in range(4):
                try:
                    f.write(urllib.request.urlopen(template.format(number)).read())
                    break
                except urllib.error.HTTPError:
                    # ponytail: stray 403s on long runs; a URL that stays 403 has expired, re-run the script
                    if attempt == 3:
                        raise
                    time.sleep(3)


def grab(video_url: str, start: str, end: str, out_path: Path, audio_only: bool):
    first, last = to_seconds(start) // SEGMENT_SECONDS, to_seconds(end) // SEGMENT_SECONDS
    print(f"grabbing pieces [{first}-{last}] -> '{out_path}'")
    if audio_only:
        # raw AAC pieces join end to end into one .aac file, enough for the loudness scan and transcripts
        fetch_pieces(piece_url_template(video_url, FORMATS["audio"]), first, last, out_path)
        print(f"'{out_path}': done")
        return
    parts = {}
    for kind, format_id in FORMATS.items():
        parts[kind] = out_path.with_suffix(f".{kind}.part")
        fetch_pieces(piece_url_template(video_url, format_id), first, last, parts[kind])
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(parts["video"]), "-i", str(parts["audio"]),
                    "-map", "0:v", "-map", "1:a", "-c", "copy", str(out_path)], check=True)
    for part in parts.values():
        part.unlink()
    print(f"'{out_path}': done")


if __name__ == "__main__":
    # arguments: the live video URL, start and end as stream time (H:MM:SS), and the output mp4
    parser = argparse.ArgumentParser(allow_abbrev=False, description="Cut a time range out of a running YouTube live")
    parser.add_argument("url", help="YouTube live URL")
    parser.add_argument("start", help="stream time where the clip starts, H:MM:SS")
    parser.add_argument("end", help="stream time where the clip ends, H:MM:SS")
    parser.add_argument("out", type=Path, help="output .mp4, or .aac with --audio-only")
    parser.add_argument("--audio-only", action="store_true", help="fetch only the audio, for finding moments")
    arguments = parser.parse_args()
    assert to_seconds("1:02:03") == 3723
    grab(arguments.url, arguments.start, arguments.end, arguments.out, arguments.audio_only)
