"""Writes site/clips.html from clips/clips.json and copies each clip's thumbnail into site/img.

Run from E:\\Cliping:  D:/Tools/youtube-env/Scripts/python scripts/build_site.py
Thumbnails come from clips/.thumbs (made by the clip tool); a clip without one is left out.
"""
import html
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clip_store import CLIPS_DIR, load_clips  # noqa: E402

SITE = CLIPS_DIR.parent / "site"
THUMBS = CLIPS_DIR / ".thumbs"
# the owner's priority creator, IShowSpeed, is listed first and marked
GROUPS = [("speed", "IShowSpeed"), ("tomo", "Tomo"), ("kai", "Kai")]
PRIORITY = "speed"
KEEP_CAPS = {"GTA": "GTA", "TOMO": "Tomo", "KAI": "Kai", "KAI'S": "Kai's", "SPEED": "Speed", "LUCIA": "Lucia"}

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clips - ViralPulse HQ</title>
<meta name="description" content="Every clip ViralPulse HQ has cut so far, grouped by streamer.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,100..900&amp;display=swap">
<link rel="icon" href="favicon.png" type="image/png">
<link rel="stylesheet" href="style.css">
</head>
<body>
<div class="container">
  <nav class="nav" aria-label="Main">
    <a class="wordmark" href="/">ViralPulse<b>HQ</b></a>
<ul>
      <li><a href="/">Home</a></li>
      <li><a href="/clips.html" aria-current="page">Clips</a></li>
      <li><a href="/#uploader">Uploader</a></li>
      <li><a href="/privacy.html">Privacy</a></li>
    </ul>
  </nav>

  <header class="page-head">
    <h1>Every clip so far</h1>
    <p class="lede">{count} short clips from Tomo, Kai and IShowSpeed streams.</p>
  </header>
{groups}
  <footer class="foot">
    <p class="big">ViralPulse<b>HQ</b></p>
    <div class="row">
      <a href="/privacy.html">Privacy policy</a>
      <p>Unofficial fan clips. Not affiliated with the streamers or Rockstar Games.</p>
    </div>
  </footer>
</div>
</body>
</html>
"""


def sentence_case(title: str) -> str:
    """'KAI'S POWER WENT OUT' -> "Kai's power went out"; known names and GTA keep their form."""
    words = [KEEP_CAPS.get(word, word.lower()) for word in title.split()]
    words[0] = words[0][0].upper() + words[0][1:]
    return " ".join(words)


def figure(clip) -> str:
    name = html.escape(clip["name"])
    title = html.escape(sentence_case(clip["title"])).replace("&#x27;", "’")
    return (f'<li><figure><img class="clip" src="img/{name}.jpg" width="270" height="480" alt="" loading="lazy">'
            f"<figcaption>{title}</figcaption></figure></li>")


def build() -> int:
    clips = [clip for clip in load_clips() if (THUMBS / f"{clip['name']}.jpg").is_file()]
    (SITE / "img").mkdir(exist_ok=True)
    for clip in clips:
        shutil.copyfile(THUMBS / f"{clip['name']}.jpg", SITE / "img" / f"{clip['name']}.jpg")
    groups = ""
    for streamer, label in GROUPS:
        mine = [clip for clip in clips if clip["streamer"] == streamer]
        if mine:
            groups += (f'\n  <section class="group{" priority" if streamer == PRIORITY else ""}" aria-labelledby="g-{streamer}">\n    <h2 id="g-{streamer}">{label}</h2>\n'
                       f'    <ul class="grid">\n      {chr(10).join("      " + figure(c) for c in mine).strip()}\n    </ul>\n  </section>\n')
    (SITE / "clips.html").write_text(PAGE.format(count=len(clips), groups=groups), encoding="utf8")
    return len(clips)


if __name__ == "__main__":
    print(f"wrote site/clips.html with {build()} clips")
