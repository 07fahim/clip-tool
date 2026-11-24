"""Loudest moments in an audio/video file (reaction screams).
Usage: python scripts/peaks.py FILE [N]   -> prints top N peaks (default 12), at least 60 s apart.
"""
import re
import subprocess
import sys

# peaks closer than this are one moment; a scream lasts several seconds
MIN_SPACING_SECONDS = 60


def parse_loudness(log: str) -> dict:
    # the loudest momentary reading of each second of an ffmpeg ebur128 log
    loudness = {}
    for second, momentary in re.findall(r"t:\s*([\d.]+)\s+TARGET.*?M:\s*(-?[\d.]+)", log):
        key = int(float(second))
        loudness[key] = max(loudness.get(key, float("-inf")), float(momentary))
    return loudness


def pick_peaks(loudness_by_second: dict, count: int) -> list:
    loudest_first = sorted(((lufs, second) for second, lufs in loudness_by_second.items()), reverse=True)
    picked = []
    for lufs, second in loudest_first:
        if all(abs(second - other) >= MIN_SPACING_SECONDS for other, _ in picked):
            picked.append((second, lufs))
        if len(picked) == count:
            break
    return sorted(picked)


def loudest_moments(source: str, count: int) -> list:
    result = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", source, "-vn", "-af", "ebur128", "-f", "null", "-"],
                            capture_output=True, text=True)
    loudness = parse_loudness(result.stderr)
    if result.returncode != 0 or not loudness:
        # no peaks from a broken file would look like a quiet video and be saved as scanned
        raise RuntimeError(f"ffmpeg found no loudness readings in '{source}'")
    return pick_peaks(loudness, count)


if __name__ == "__main__":
    wanted = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    for second, lufs in loudest_moments(sys.argv[1], wanted):
        print(f"{second // 3600}:{second % 3600 // 60:02}:{second % 60:02}  {lufs:.1f} LUFS")
