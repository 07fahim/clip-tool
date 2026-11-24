"""Make OpusClip-style vertical clips from the Speed x Expedia footage with ffmpeg only.

Layouts: "face" = Speed fills 9:16 frame; "split" = Speed on top, phone call below.
Captions come from YouTube auto-captions (footage/expedia_lines.txt: "<sec> <text>").
Run from E:\\Cliping:  python scripts/make_clips.py
"""
import html
import subprocess
from pathlib import Path

SRC = "footage/speed_expedia_from_2-46-29.mp4"
LOGO = "assets/Expedia_Logo-RegistrationMark-Horizontal-Inverted.png"
OUT = Path("clips/v2")
FONT = "C\\:/Windows/Fonts/arialbd.ttf"

# Crops measured on the 1280x720 source (face ~x580; phone ~x780-930, y280-550).
CROP_FACE = "crop=640:720:260:0"   # wide enough that Speed stays in frame
CROP_TOP = "crop=640:569:260:20"   # 9:8 -> 1080x960
CROP_PHONE = "crop=300:267:705:283"

CLIPS = [  # name, layout, title, subtitle, segments (seconds in source file)
    ("1_hongkong_10k", "split", "HE WON $10,000 FOR HONG KONG", "IShowSpeed x Expedia", [(264, 268), (214, 240), (252, 302)]),
    ("2_winners_revealed", "face", "SPEED REVEALS THE WINNERS", "Expedia trips: Japan + Hong Kong", [(318, 336)]),
    ("3_going_to_japan", "split", "HE IS GOING TO JAPAN!", "IShowSpeed x Expedia", [(98, 101), (45, 70), (82, 113), (129, 141)]),
    ("4_dream_trips", "face", "SPEED x EXPEDIA DREAM TRIPS", "Fans win trips to Japan + Hong Kong", [(18, 45), (113, 131)]),
    ("5_learn_cantonese", "split", "GOING TO HONG KONG?", "Learn Cantonese, bro!", [(264, 295)]),
    ("6_japan_story", "split", "WHY HE WANTS TO GO TO JAPAN", "IShowSpeed x Expedia", [(45, 101)]),
]


WORDS_PER_CUE = 3


def load_lines():
    """Caption cues (start, end, text). Prefer word timings from scripts/words.py (SRC.words.json)."""
    words_file = Path(SRC).with_suffix(".words.json")
    if words_file.exists():
        import json
        w = json.loads(words_file.read_text(encoding="utf8"))
        return [(g[0][0], g[-1][1], " ".join(x[2] for x in g))
                for g in (w[i:i + WORDS_PER_CUE] for i in range(0, len(w), WORDS_PER_CUE))]
    rows = []
    for raw in Path("footage/expedia_lines.txt").read_text(encoding="utf8").splitlines():
        sec, _, text = raw.partition(" ")
        rows.append((int(sec), html.unescape(text).replace(">>", "").strip()))
    return [(s, rows[i + 1][0] if i + 1 < len(rows) else s + 2, t) for i, (s, t) in enumerate(rows)]


def ts(x):
    return f"{int(x // 3600):02}:{int(x % 3600 // 60):02}:{int(x % 60):02},{int(x % 1 * 1000):03}"


def cut_cues(segs, lines):
    # the caption cues that fall inside the segments, in clip time and in capitals as they are shown
    cues, offset = [], 0
    for a, b in segs:
        for s, e, t in lines:
            s2, e2 = max(s, a), min(e, b)
            if e2 > s2 and t:
                cues.append((s2 - a + offset, e2 - a + offset, t.upper()))
        offset += b - a
    return cues


def write_srt(path, segs, lines):
    write_cues(path, cut_cues(segs, lines))


def write_cues(path, cues):
    out = [f"{n}\n{ts(s)} --> {ts(e)}\n{t}\n" for n, (s, e, t) in enumerate(cues, 1)]
    if not out:
        # a clip with no speech: the subtitles filter cannot open an empty file, so give it one invisible cue
        out.append("1\n00:00:00,000 --> 00:00:00,100\n\u00a0\n")
    path.write_text("\n".join(out), encoding="utf8")


def make(name, layout, title, sub, segs, lines):
    OUT.mkdir(parents=True, exist_ok=True)
    srt, t1, t2 = OUT / f"{name}.srt", OUT / f"{name}_t1.txt", OUT / f"{name}_t2.txt"
    write_srt(srt, segs, lines)
    t1.write_text(title, encoding="utf8")
    t2.write_text(sub, encoding="utf8")
    f = "".join(f"[0:v]trim={a}:{b},setpts=PTS-STARTPTS[v{i}];[0:a]atrim={a}:{b},asetpts=PTS-STARTPTS[a{i}];"
                for i, (a, b) in enumerate(segs))
    f += "".join(f"[v{i}][a{i}]" for i in range(len(segs))) + f"concat=n={len(segs)}:v=1:a=1[v][a];"
    if layout == "face":
        f += f"[v]split[b][c];[b]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:2[bg];[c]{CROP_FACE},scale=1080:-2[fg];[bg][fg]overlay=0:(H-h)/2[base];"
        cap_y = 70    # libass units (288 = full height)
    else:
        f += f"[v]split[t][p];[t]{CROP_TOP},scale=1080:960[top];[p]{CROP_PHONE},scale=1080:960[bot];[top][bot]vstack[base];"
        cap_y = 120   # just above the seam
    style = f"FontName=Arial,Bold=1,FontSize=11,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=3,Shadow=0,Alignment=2,MarginV={cap_y}"
    f += f"[1:v]scale=300:-1[lg];[base][lg]overlay=(W-w)/2:H-h-90[wl];"
    f += f"[wl]subtitles={srt.as_posix()}:original_size=1080x1920:force_style='{style}',"
    f += (f"drawtext=fontfile='{FONT}':textfile={t1.as_posix()}:fontsize=54:fontcolor=white:borderw=7:bordercolor=black:x=(w-tw)/2:y=330,"
          f"drawtext=fontfile='{FONT}':textfile={t2.as_posix()}:fontsize=44:fontcolor=yellow:borderw=6:bordercolor=black:x=(w-tw)/2:y=405[outv]")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", SRC, "-i", LOGO, "-filter_complex", f, "-map", "[outv]", "-map", "[a]",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-b:a", "160k",
                    "-movflags", "+faststart", str(OUT / f"{name}.mp4")], check=True)


if __name__ == "__main__":
    lines = load_lines()
    for c in CLIPS:
        make(*c, lines)
        print("done", c[0])
