import json
import logging
import re
import os
import subprocess
import sys
import time
from pathlib import Path

from clip_store import (CLIPS_DIR, PICKS, caption_style, check_caption_preset, check_layout_preset, load_clips,
                        load_presets)
from caption_timing import (MAX_WORD_SECONDS, SPEECH_WORD_LIMIT_SECONDS, apply_fixes, apply_timing, cue_key, group_words,
                            merge_words, uncovered_speech)  # the first two are re-exported for the tests
from make_clips import cut_cues, write_cues
from whisper_settings import WHISPER_PYTHON

FONT = "C\\:/Windows/Fonts/arialbd.ttf"
# extra fonts (Google Fonts, open licence) for captions and titles; relative to the folder the tool runs from, like clips/
FONTS_DIR = "fonts"
# a render or a Whisper run that has not ended after this long is stuck; without a limit it would hold the tool's
# one heavy-job slot until restart
RENDER_TIMEOUT_SECONDS = 1800
# Windows lets an antivirus scan, the thumbnail maker or a player hold a fresh file for a moment (seen 2026-11-24: the swap of a
# finished render failed with 'access denied'), so a held file is tried again a few times before the render counts as failed
LOCK_TRIES, LOCK_WAIT_SECONDS = 5, 2
# how the clip video is encoded. Timed 2026-11-23 on a real 31 s clip: libx264 medium crf 20 took 106 s (file 32 MB), veryfast crf 23
# took 43 s (20 MB); the GTX 1050 h264_nvenc failed on this machine. 30 fps saved only 17% more, so the 60 fps of the stream is kept.
# medium crf 20 chosen 2026-11-24 by the owner's word: clips must be high quality; render time is not a limit for a night batch
VIDEO_ENCODER_ARGS = ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]
OUTPUT_FPS_ARGS = []
WORDS_TIMEOUT_SECONDS = 3600
# the speech detector takes seconds on a 60 s pick; a stuck run must not hold the render slot
SPEECH_TIMEOUT_SECONDS = 600
# one Whisper pass over a few short stretches
RECOVER_TIMEOUT_SECONDS = 900

logger = logging.getLogger("make_gta_clips")

# layouts and caption styles live in clips/presets.json, the clip list in clips/clips.json (see clip_store.py).
# a layout is (facecam crop, facecam height on the 1080x1920 frame, gameplay crop) on the 1920x1080 stream; the gameplay
# crop has the ratio of the space left under the cam. type "face" = the facecam fills the whole frame; type "fullframe"
# (Speed) = gameplay fills the frame and the cam is a small bordered box, whose width is cam_height; it grows to
# big_width during the clip's big_at seconds
# Speed's cam box height follows the cam's 388:240 ratio

# slurs and swears are starred in the burned-in captions: on-screen slurs cut TikTok reach and Kai's rules ban
# anything that reads as hate; the audio stays as said
CENSORED = [(r"(?i)\bnigg\w*", "n****"), (r"(?i)fuck", "f**k"), (r"(?i)\bbitch", "b***h")]


def load_cues(stem: str, fixes: dict = None):
    words_path = PICKS / f"{stem}.words.json"
    if not words_path.exists():
        # words.py needs faster-whisper, which only lives in the whisper env (D:/Tools/whisper-env)
        environment = dict(os.environ, HF_HOME="D:\\caches\\huggingface")
        subprocess.run([WHISPER_PYTHON, "scripts/words.py", str(PICKS / f"{stem}.mp4"), str(words_path)],
                       check=True, env=environment, timeout=WORDS_TIMEOUT_SECONDS)
    words = json.loads(words_path.read_text(encoding="utf8"))
    speech = load_speech(stem)
    words = apply_fixes(merge_words(words, load_recovered_words(stem, words, speech)), fixes)
    return [(start, end, censor(text)) for start, end, text in group_words(words, speech)]


def final_cues(clip: dict, lines: list) -> list:
    return apply_timing(cut_cues(clip["segments"], lines), clip.get("caption_timing"))


def clip_captions(clip: dict) -> list:
    # the lines the clip will show, in clip time; nothing for a pick whose words were never made (making them takes minutes)
    if not (PICKS / f"{clip['pick']}.words.json").exists():
        return []
    lines = load_cues(clip["pick"], clip.get("caption_fixes"))
    cut = cut_cues(clip["segments"], lines)
    # the key is the cue's start before any timing change, so the page can name the cue it edits
    return [(start, end, text, cue_key(original[0])) for original, (start, end, text) in zip(cut, final_cues(clip, lines))]


def load_speech(stem: str) -> list:
    speech_path = PICKS / f"{stem}.speech.json"
    if not speech_path.exists():
        try:
            subprocess.run([WHISPER_PYTHON, "scripts/speech_spans.py", str(PICKS / f"{stem}.mp4"), str(speech_path)],
                           check=True, env=dict(os.environ, HF_HOME="D:\\caches\\huggingface"), timeout=SPEECH_TIMEOUT_SECONDS)
        except (subprocess.SubprocessError, OSError) as error:
            # the spans only sharpen the caption ends, so a clip is still rendered without them (long words are cut to 1.2 s)
            logger.warning(f"speech spans not made for '{stem}': {error!r}")
            return []
    return json.loads(speech_path.read_text(encoding="utf8"))


def load_recovered_words(stem: str, words: list, speech: list) -> list:
    if not uncovered_speech(words, speech):
        return []
    recovered_path = PICKS / f"{stem}.recovered.json"
    if not recovered_path.exists():
        try:
            subprocess.run([WHISPER_PYTHON, "scripts/recover_words.py", str(PICKS / f"{stem}.mp4"), str(PICKS / f"{stem}.words.json"),
                            str(PICKS / f"{stem}.speech.json"), str(recovered_path)],
                           check=True, env=dict(os.environ, HF_HOME="D:\\caches\\huggingface"), timeout=RECOVER_TIMEOUT_SECONDS)
        except (subprocess.SubprocessError, OSError) as error:
            # speech the first pass dropped stays without a caption, as before; the render goes on
            logger.warning(f"missed speech not recovered for '{stem}': {error!r}")
            return []
    return json.loads(recovered_path.read_text(encoding="utf8"))


def censor(text: str):
    for pattern, stars in CENSORED:
        text = re.sub(pattern, stars, text)
    return text


def retry_while_locked(action, wait_seconds: float = LOCK_WAIT_SECONDS):
    for attempt in range(LOCK_TRIES):
        try:
            return action()
        except PermissionError:
            if attempt == LOCK_TRIES - 1:
                raise
            time.sleep(wait_seconds)


def title_box_rect(title_text: str, title_y: int):
    """Where the title's highlighter box sits (x, y, width, height): draws it white on black once and measures it."""
    measure = subprocess.run(["ffmpeg", "-v", "info", "-f", "lavfi", "-i", "color=black:s=1080x1920:d=0.3:r=10", "-vf",
                              f"{title_text}:fontcolor=white:box=1:boxcolor=white:boxborderw=16:x=(w-tw)/2:y={title_y},bbox=min_val=128",
                              "-f", "null", "-"], capture_output=True, text=True, check=True, timeout=60)
    found = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", measure.stderr)
    if not found:
        raise RuntimeError("could not measure the title box for the gradient")
    width, height, x, y = (int(value) for value in found[-1])
    return x, y, width, height


def render_progress(clip: dict, out_dir: Path = CLIPS_DIR):
    """The share of the clip already rendered (0 to 1), read from the progress file ffmpeg writes while it works."""
    path = out_dir / clip["streamer"] / f"{clip['name']}.progress"
    try:
        done = re.findall(r"out_time_us=(\d+)", path.read_text(encoding="utf8"))
    except OSError:
        return 0.0
    total = sum(end - start for start, end in clip["segments"])
    return min(int(done[-1]) / 1e6 / total, 1.0) if done and total > 0 else 0.0


def make_clip(clip: dict, out_dir: Path = CLIPS_DIR):
    presets = load_presets()
    layout, caption = presets["layouts"][clip["layout"]], presets["captions"][clip["caption"]]
    # the values go into the ffmpeg filter; presets.json can also be edited by hand, so check them again here
    problems = check_caption_preset(caption) + check_layout_preset(layout)
    if problems:
        raise ValueError("; ".join(problems))
    if "caption_margin_v" in clip:
        caption = dict(caption, margin_v=clip["caption_margin_v"])
    if "caption_font" in clip:
        caption = dict(caption, font=clip["caption_font"])
    title_font = clip.get("title_font", caption.get("title_font"))
    if title_font and not (Path(FONTS_DIR) / title_font).is_file():
        raise ValueError(f"title_font: no file {title_font} in the {FONTS_DIR} folder")
    name, segments = clip["name"], clip["segments"]
    out = out_dir / clip["streamer"]
    out.mkdir(parents=True, exist_ok=True)
    srt, title_file, bottom_file = out / f"{name}.srt", out / f"{name}_t1.txt", out / f"{name}_t2.txt"
    write_cues(srt, final_cues(clip, load_cues(clip["pick"], clip.get("caption_fixes"))))
    title_file.write_text(clip["title"], encoding="utf8")
    bottom_file.write_text(clip["bottom_line"], encoding="utf8")
    # a clip may carry its own camera crop (cams move within a stream); it beats the layout's
    cam_crop, cam_height, game_crop = clip.get("cam_crop") or layout["cam_crop"], layout.get("cam_height"), layout.get("game_crop")
    graph = "".join(f"[0:v]trim={a}:{b},setpts=PTS-STARTPTS[v{i}];[0:a]atrim={a}:{b},asetpts=PTS-STARTPTS[a{i}];"
                    for i, (a, b) in enumerate(segments))
    graph += "".join(f"[v{i}][a{i}]" for i in range(len(segments))) + f"concat=n={len(segments)}:v=1:a=1[v][a];"
    if layout["type"] == "fullframe":
        def box(width):
            return f"scale={width}:{width * 240 // 388 // 2 * 2},pad=iw+12:ih+12:6:6:color=red"
        graph += f"[v]split[c][g];[g]{game_crop},scale=1080:1920[game];"
        if clip.get("big_at"):
            during = "between(t,{},{})".format(*clip["big_at"])
            graph += (f"[c]{cam_crop},split[cs][cb];[cs]{box(cam_height)}[small];[cb]{box(layout['big_width'])}[big];"
                      f"[game][small]overlay=30:200:enable='not({during})'[g1];"
                      f"[g1][big]overlay=30:200:enable='{during}'[base];")
        else:
            graph += f"[c]{cam_crop},{box(cam_height)}[cam];[game][cam]overlay=30:200[base];"
        title_y = 110
    elif layout["type"] == "split":
        graph += (f"[v]split[c][g];[c]{cam_crop},scale=1080:{cam_height}[cam];"
                  f"[g]{game_crop},scale=1080:{1920 - cam_height}[game];[cam][game]vstack[base];")
        title_y = layout.get("title_y", cam_height + 30)
    else:
        graph += f"[v]{cam_crop},scale=1080:1920[base];"
        title_y = 300
    title_y = clip.get("title_y", title_y)
    # MarginV is in libass units (288 = full height, ~6.7 px each); 30 keeps captions low, just above the
    # bottom line at y=1790, so they do not cover the middle of the gameplay. Speed's preset sits higher (60, ~400 px
    # up) because his full-frame gameplay puts the game's own subtitles at y~1720, right where 30 lands
    title_font_file = f"{FONTS_DIR}/{title_font}" if title_font else FONT
    title_text = f"drawtext=expansion=none:fontfile='{title_font_file}':textfile={title_file.as_posix()}:fontsize=52"
    graph += f"[base]subtitles={srt.as_posix()}:original_size=1080x1920:fontsdir={FONTS_DIR}:force_style='{caption_style(caption)}'"
    # a title box (highlighter) replaces the black outline: red or white text on a busy game picture was hard to read
    if caption.get("title_box_to"):
        # drawtext boxes are one flat colour, so a gradient box is a gradient picture of the same size laid under the plain title
        x, y, width, height = title_box_rect(title_text, title_y)
        graph += (f"[sub];gradients=s={width}x{height}:c0={caption['title_box']}:c1={caption['title_box_to']}:x0=0:y0=0:x1={width}:y1=0:d=1[grad];"
                  f"[sub][grad]overlay={x}:{y}[boxed];[boxed]")
        title_look = "borderw=0"
    else:
        graph += ","
        title_look = (f"box=1:boxcolor={caption['title_box']}@0.95:boxborderw=16" if caption.get("title_box")
                      else "borderw=7:bordercolor=black")
    graph += (f"{title_text}:fontcolor={caption['title_colour']}:"
              f"{title_look}:x=(w-tw)/2:y={title_y},"
              f"drawtext=expansion=none:fontfile='{FONT}':textfile={bottom_file.as_posix()}:fontsize=40:fontcolor={caption['bottom_colour']}:borderw=6:"
              f"bordercolor=black:x=(w-tw)/2:y=1790[outv]")
    output = out / f"{name}.mp4"
    # render beside the final file and swap on success, so a failed run cannot leave a broken file in its place
    temporary = out / f"{name}.tmp.mp4"
    progress_file = out / f"{name}.progress"
    try:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(PICKS / f"{clip['pick']}.mp4"), "-filter_complex", graph,
                        "-progress", str(progress_file), "-nostats", "-map", "[outv]", "-map", "[a]", *VIDEO_ENCODER_ARGS, *OUTPUT_FPS_ARGS,
                        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(temporary)], check=True, timeout=RENDER_TIMEOUT_SECONDS)
        retry_while_locked(lambda: os.replace(temporary, output))
    finally:
        retry_while_locked(lambda: temporary.unlink(missing_ok=True))
        retry_while_locked(lambda: progress_file.unlink(missing_ok=True))
    return output


if __name__ == "__main__":
    assert censor("Come here, nigga. Fuck it, bitch") == "Come here, n****. f**k it, b***h"
    # optional arguments: clip names to make; none = all
    wanted = set(sys.argv[1:])
    clips = [clip for clip in load_clips() if not wanted or clip["name"] in wanted]
    print(f"making [{len(clips)}] clips")
    for done, clip in enumerate(clips, 1):
        make_clip(clip)
        print(f"[{done}/{len(clips)}] '{clip['name']}': done")
