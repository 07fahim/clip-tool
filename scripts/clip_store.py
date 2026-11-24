import json
import math
import os
import re
from pathlib import Path

PICKS = Path("footage/picks")
CLIPS_DIR = Path("clips")
PRESETS_PATH = Path("clips/presets.json")
CLIPS_PATH = Path("clips/clips.json")
# a name becomes a file or folder name, so no dots or slashes that could leave the clips folder
NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
# longer titles run off the 1080 px frame at fontsize 52
MAX_TITLE_LENGTH = 28
# a per-clip camera crop goes straight into the ffmpeg filter, so only a plain crop=w:h:x:y is allowed
CAM_CROP_PATTERN = re.compile(r"crop=\d+:\d+:\d+:\d+")
# the only keys a clip keeps; anything else a request carries is dropped
CLIP_KEYS = ("name", "streamer", "pick", "title", "bottom_line", "segments", "caption", "layout", "cam_crop", "big_at",
             "hook", "body", "extra_tags", "own_tags_only", "cam_unverified", "caption_fixes", "caption_timing", "title_y", "caption_margin_v", "caption_font", "title_font", "review")
# YouTube allows 100 characters in a title
MAX_HOOK_LENGTH = 100
# shortcut: 15 tags on the YouTube line is a guess, not checked against YouTube's rules;
# lower it if a post shows a problem
MAX_YOUTUBE_TAGS = 15
TAG_PATTERN = re.compile(r"#?[A-Za-z0-9_]+")
# a caption fix goes into the subtitle file, so only plain words are allowed (no braces or backslashes: those are
# subtitle commands); the right word may be empty, which removes the wrong word
FIX_WORD_PATTERN = re.compile(r"[A-Za-z0-9'’-]{1,30}")
MAX_CAPTION_FIXES = 20
# a caption timing change is stored under the cue's start in the clip, as seconds with two decimals
CUE_KEY_PATTERN = re.compile(r"\d{1,5}\.\d{2}")
MAX_CAPTION_TIMINGS = 100
MAX_CAPTION_SECONDS = 600
REVIEWS = ("approved", "rejected")
PLATFORMS = ("youtube", "facebook")
# a creator's own tag list; more than this is a typo or a paste of the wrong thing
MAX_CREATOR_TAGS = 30
PRESET_KINDS = ("captions", "layouts")
# these go into the ffmpeg style string and filter, so only plain values pass: no commas, colons or quotes
FONT_PATTERN = re.compile(r"[A-Za-z0-9 ]{1,40}")
ASS_COLOUR_PATTERN = re.compile(r"&H[0-9A-Fa-f]{8}")
FFMPEG_COLOUR_PATTERN = re.compile(r"[a-z]{3,20}|#[0-9A-Fa-f]{6}")
CREATOR_FIELDS = ("youtube", "facebook", "caption", "layout")
CAPTION_FIELDS = ("font", "size", "primary", "outline_colour", "outline", "margin_v", "title_colour", "bottom_colour")
# shortcut: these limits are guesses that keep text and boxes inside the 1080x1920 frame;
# widen them if a real style needs it
CAPTION_NUMBERS = {"size": (5, 60), "outline": (0, 10), "margin_v": (0, 600)}
# a clip's own caption height: the subtitle units run 0 to 288 for the whole frame height (about 6.7 px each), so 250 is near the top
CLIP_CAPTION_HEIGHT = (0, 250)
LAYOUT_NUMBERS = {"cam_height": (100, 1700), "big_width": (100, 1080), "title_y": (0, 1700)}
# a split layout puts its title on the seam unless it says where; the field may be left out
OPTIONAL_LAYOUT_FIELDS = ("title_y",)
# a caption style draws its title in a highlighter box of this colour when it says so; the field may be left out
OPTIONAL_CAPTION_FIELDS = ("title_box", "title_box_to", "title_font")
# a title font is a file name in the fonts folder, used by drawtext; no path, so no way to point outside the folder
TITLE_FONT_PATTERN = re.compile(r"[A-Za-z0-9_-]+\.ttf")
# the creator's line at the bottom of every new clip; one line, drawn from a text file, so any plain text is safe
MAX_BOTTOM_LINE = 40
LAYOUT_FIELDS = {"face": ("cam_crop",), "split": ("cam_crop", "cam_height", "game_crop", "title_y"),
                 "fullframe": ("cam_crop", "cam_height", "game_crop", "big_width")}


def load_presets():
    return json.loads(PRESETS_PATH.read_text(encoding="utf8"))


def caption_style(preset: dict):
    return (f"FontName={preset['font']},Bold=1,FontSize={preset['size']},PrimaryColour={preset['primary']},"
            f"OutlineColour={preset['outline_colour']},Outline={preset['outline']},Shadow=0,Alignment=2,"
            f"MarginV={preset['margin_v']}")


def is_plain_name(value):
    return isinstance(value, str) and NAME_PATTERN.fullmatch(value) is not None


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def check_segments(segments):
    if not isinstance(segments, list) or not segments:
        return ["segments: at least one [start, end] pair is needed"]
    problems = []
    previous_end = 0
    for pair in segments:
        if not (isinstance(pair, (list, tuple)) and len(pair) == 2 and all(is_number(value) for value in pair)):
            problems.append("segments: each one must be a [start, end] pair of numbers")
            continue
        start, end = pair
        if start < 0 or end <= start:
            problems.append(f"segment [{start}, {end}]: needs 0 <= start < end")
        elif start < previous_end:
            problems.append(f"segment [{start}, {end}]: overlaps or comes before the one above it")
        previous_end = max(previous_end, end)
    return problems


def check_clip(clip, picks_dir: Path = PICKS):
    if not isinstance(clip, dict):
        return ["clip: must be an object"]
    problems = []
    if not is_plain_name(clip.get("name")):
        problems.append("name: letters, digits, _ and - only")
    if not is_plain_name(clip.get("streamer")):
        problems.append("streamer: letters, digits, _ and - only")
    title = clip.get("title")
    if not isinstance(title, str) or not title or len(title) > MAX_TITLE_LENGTH:
        problems.append(f"title: 1 to {MAX_TITLE_LENGTH} characters")
    if not isinstance(clip.get("bottom_line", ""), str):
        problems.append("bottom_line: must be text")
    pick = clip.get("pick")
    if not is_plain_name(pick) or not (picks_dir / f"{pick}.mp4").is_file():
        problems.append(f"pick: no file {pick}.mp4 in {picks_dir}")
    presets = load_presets()
    if not isinstance(clip.get("caption"), str) or clip["caption"] not in presets["captions"]:
        problems.append(f"caption: unknown preset '{clip.get('caption')}'")
    if not isinstance(clip.get("layout"), str) or clip["layout"] not in presets["layouts"]:
        problems.append(f"layout: unknown preset '{clip.get('layout')}'")
    if "cam_crop" in clip and not (isinstance(clip["cam_crop"], str) and CAM_CROP_PATTERN.fullmatch(clip["cam_crop"])):
        problems.append("cam_crop: use crop=width:height:x:y with whole numbers")
    if "big_at" in clip:
        big_at = clip["big_at"]
        if not (isinstance(big_at, list) and len(big_at) == 2 and all(is_number(value) for value in big_at)
                and 0 <= big_at[0] < big_at[1]):
            problems.append("big_at: two numbers, [from, to] seconds, from before to")
    # the clip's own title height (pixels from the top) and caption height (as in the caption style); left out = the layout's and style's
    for key, (low, high) in (("title_y", LAYOUT_NUMBERS["title_y"]), ("caption_margin_v", CLIP_CAPTION_HEIGHT)):
        if key in clip and not (isinstance(clip[key], int) and not isinstance(clip[key], bool) and low <= clip[key] <= high):
            problems.append(f"{key}: a whole number from {low} to {high}")
    # the owner's verdict on a generated clip; left out = not looked at yet
    if "review" in clip and clip["review"] not in REVIEWS:
        problems.append("review: approved or rejected")
    if "caption_font" in clip and not (isinstance(clip["caption_font"], str) and FONT_PATTERN.fullmatch(clip["caption_font"])):
        problems.append("caption_font: a font name, letters, digits and spaces only")
    if "title_font" in clip and not (isinstance(clip["title_font"], str) and TITLE_FONT_PATTERN.fullmatch(clip["title_font"])):
        problems.append("title_font: a .ttf file name from the fonts folder")
    hook = clip.get("hook", "x")
    # a title cannot start or end with spaces or hold a line break or tab
    if not (isinstance(hook, str) and 1 <= len(hook) <= MAX_HOOK_LENGTH and hook == hook.strip()
            and not any(character in hook for character in "\n\r\t")):
        problems.append(f"hook: 1 to {MAX_HOOK_LENGTH} characters of one line of text, no spaces at the ends")
    if not isinstance(clip.get("body", ""), str):
        problems.append("body: must be text")
    tags = clip.get("extra_tags", [])
    # the YouTube line holds 15 tags and #shorts takes one, so more would be dropped silently
    if not (isinstance(tags, list) and len(tags) <= MAX_YOUTUBE_TAGS - 1
            and all(isinstance(tag, str) and TAG_PATTERN.fullmatch(tag) for tag in tags)):
        problems.append(f"extra_tags: a list of at most {MAX_YOUTUBE_TAGS - 1} tags, letters, digits and _ only")
    # true = this clip uses only its own tags: the creator's list is about one game (Speed's is GTA 6) and would mislabel
    # a clip from another game
    fixes = clip.get("caption_fixes", {})
    if not (isinstance(fixes, dict) and len(fixes) <= MAX_CAPTION_FIXES
            and all(isinstance(wrong, str) and FIX_WORD_PATTERN.fullmatch(wrong) and isinstance(right, str)
                    and (right == "" or FIX_WORD_PATTERN.fullmatch(right)) for wrong, right in fixes.items())):
        problems.append(f"caption_fixes: at most {MAX_CAPTION_FIXES} plain words, each with a plain right word or none")
    timing = clip.get("caption_timing", {})
    if not (isinstance(timing, dict) and len(timing) <= MAX_CAPTION_TIMINGS and all(
            isinstance(key, str) and CUE_KEY_PATTERN.fullmatch(key) and isinstance(change, dict)
            and is_number(change.get("start")) and is_number(change.get("end"))
            and 0 <= change["start"] < change["end"] <= MAX_CAPTION_SECONDS for key, change in timing.items())):
        problems.append("caption_timing: a start and an end in seconds, start first, for each caption")
    for key in ("own_tags_only", "cam_unverified"):
        if key in clip and not isinstance(clip[key], bool):
            problems.append(f"{key}: true or false")
    return problems + check_segments(clip.get("segments"))


def join_tags(tags: list):
    # one '#' each, no repeats (case-insensitive), first spelling and order kept
    seen = set()
    joined = []
    for tag in tags:
        tag = "#" + tag.lstrip("#")
        if tag.lower() not in seen:
            seen.add(tag.lower())
            joined.append(tag)
    return joined


def build_text(clip: dict, platform: str, presets: dict):
    if platform not in PLATFORMS:
        raise ValueError(f"platform: one of {', '.join(PLATFORMS)}")
    used_fallback = not clip.get("hook")
    # the title as written: sentence case would turn GTA and names into "Gta"
    hook = clip["title"] if used_fallback else clip["hook"]
    body = clip.get("body", "")
    streamer_tags = [] if clip.get("own_tags_only") else presets.get("creators", {}).get(clip["streamer"], {}).get(platform, [])
    extra_tags = clip.get("extra_tags", [])
    tags = join_tags(streamer_tags + extra_tags)
    if platform == "facebook":
        return {"caption": "\n".join(part for part in (hook, body, " ".join(tags)) if part),
                "used_fallback": used_fallback}
    # #shorts goes last and counts in the cap; over the cap the streamer's own tags go first, the clip's tags stay
    tags = [tag for tag in tags if tag.lower() != "#shorts"]
    clip_tags = {tag.lower() for tag in join_tags(extra_tags)}
    while len(tags) > MAX_YOUTUBE_TAGS - 1:
        own = [i for i, tag in enumerate(tags) if tag.lower() not in clip_tags]
        tags.pop(own[-1] if own else -1)
    description = " ".join(part for part in (body, " ".join(tags + ["#shorts"])) if part)
    return {"title": hook, "description": description, "used_fallback": used_fallback}


def check_numbers(preset: dict, ranges: dict):
    problems = []
    for field, (low, high) in ranges.items():
        value = preset.get(field)
        if not (isinstance(value, int) and not isinstance(value, bool) and low <= value <= high):
            problems.append(f"{field}: a whole number from {low} to {high}")
    return problems


def check_caption_preset(preset):
    if not isinstance(preset, dict):
        return ["caption style: must be an object"]
    problems = check_numbers(preset, CAPTION_NUMBERS)
    if not (isinstance(preset.get("font"), str) and FONT_PATTERN.fullmatch(preset["font"])):
        problems.append("font: a font name, letters, digits and spaces only")
    for field in ("primary", "outline_colour"):
        if not (isinstance(preset.get(field), str) and ASS_COLOUR_PATTERN.fullmatch(preset[field])):
            problems.append(f"{field}: a colour like &H00FFFFFF (blue, green, red, each 00 to FF)")
    for field in ("title_colour", "bottom_colour"):
        if not (isinstance(preset.get(field), str) and FFMPEG_COLOUR_PATTERN.fullmatch(preset[field])):
            problems.append(f"{field}: a colour name like white, or #RRGGBB")
    if "title_box" in preset and not (isinstance(preset["title_box"], str) and FFMPEG_COLOUR_PATTERN.fullmatch(preset["title_box"])):
        problems.append("title_box: a colour name like yellow, or #RRGGBB")
    if "title_font" in preset and not (isinstance(preset["title_font"], str) and TITLE_FONT_PATTERN.fullmatch(preset["title_font"])):
        problems.append("title_font: a .ttf file name from the fonts folder")
    # title_box_to turns the box into a gradient from title_box (left) to this colour (right)
    if "title_box_to" in preset and not ("title_box" in preset and isinstance(preset["title_box_to"], str)
                                         and FFMPEG_COLOUR_PATTERN.fullmatch(preset["title_box_to"])):
        problems.append("title_box_to: a colour name like blue, or #RRGGBB, and the style needs a title_box too")
    return problems


def check_layout_preset(preset):
    if not isinstance(preset, dict):
        return ["layout: must be an object"]
    if preset.get("type") not in LAYOUT_FIELDS:
        return [f"type: one of {', '.join(LAYOUT_FIELDS)}"]
    numbered = [field for field in LAYOUT_FIELDS[preset["type"]] if field in LAYOUT_NUMBERS]
    problems = check_numbers(preset, {field: LAYOUT_NUMBERS[field] for field in numbered
                                      if field in preset or field not in OPTIONAL_LAYOUT_FIELDS})
    for field in ("cam_crop", "game_crop"):
        if field in LAYOUT_FIELDS[preset["type"]] and not (
                isinstance(preset.get(field), str) and CAM_CROP_PATTERN.fullmatch(preset[field])):
            problems.append(f"{field}: use crop=width:height:x:y with whole numbers")
    return problems


def save_preset(kind: str, name: str, preset):
    if kind not in PRESET_KINDS:
        return [f"kind: one of {', '.join(PRESET_KINDS)}"]
    if not is_plain_name(name):
        return ["name: letters, digits, _ and - only"]
    removed = []
    if kind == "captions" and isinstance(preset, dict):
        # the page sends an emptied box colour as "": that removes the stored one
        removed = [field for field in OPTIONAL_CAPTION_FIELDS if preset.get(field) == ""]
        preset = {field: value for field, value in preset.items() if field not in removed}
    problems = check_caption_preset(preset) if kind == "captions" else check_layout_preset(preset)
    if problems:
        return problems
    keep = CAPTION_FIELDS + OPTIONAL_CAPTION_FIELDS if kind == "captions" else ("type",) + LAYOUT_FIELDS[preset["type"]]
    presets = load_presets()
    saved = {field: preset[field] for field in keep if field in preset}
    # the page's forms have no box for these optional fields, so saving a form keeps the ones already stored
    stored = presets[kind].get(name, {})
    for field in OPTIONAL_CAPTION_FIELDS + OPTIONAL_LAYOUT_FIELDS:
        if field in keep and field not in saved and field in stored and field not in removed:
            saved[field] = stored[field]
    presets[kind][name] = saved
    write_presets(presets)
    return []


def write_presets(presets: dict):
    # write beside the file and swap, so a crash mid-write cannot leave half a presets.json
    temporary = PRESETS_PATH.with_name(PRESETS_PATH.name + ".tmp")
    temporary.write_text(json.dumps(presets, indent=2, ensure_ascii=False) + "\n", encoding="utf8")
    os.replace(temporary, PRESETS_PATH)


def check_creator(creator, presets: dict):
    if not isinstance(creator, dict):
        return ["creator: must be an object"]
    problems = []
    for platform in PLATFORMS:
        tags = creator.get(platform)
        if not (isinstance(tags, list) and len(tags) <= MAX_CREATOR_TAGS
                and all(isinstance(tag, str) and TAG_PATTERN.fullmatch(tag) for tag in tags)):
            problems.append(f"{platform}: a list of at most {MAX_CREATOR_TAGS} tags, letters, digits and _ only")
    for field, kind in (("caption", "captions"), ("layout", "layouts")):
        if not (isinstance(creator.get(field), str) and creator[field] in presets[kind]):
            problems.append(f"{field}: pick one of the saved {kind}")
    line = creator.get("bottom_line", "")
    if not (isinstance(line, str) and len(line) <= MAX_BOTTOM_LINE and "\n" not in line):
        problems.append(f"bottom_line: one line of at most {MAX_BOTTOM_LINE} characters")
    return problems


def save_creator(name: str, creator):
    if not is_plain_name(name):
        return ["name: letters, digits, _ and - only"]
    presets = load_presets()
    problems = check_creator(creator, presets)
    if problems:
        return problems
    saved = {field: creator[field] for field in CREATOR_FIELDS}
    # the page's form does not send the bottom line, so saving the form keeps the one already stored
    line = creator.get("bottom_line", presets.get("creators", {}).get(name, {}).get("bottom_line"))
    if line:
        saved["bottom_line"] = line
    presets.setdefault("creators", {})[name] = saved
    write_presets(presets)
    return []


def load_clips():
    return json.loads(CLIPS_PATH.read_text(encoding="utf8"))


def save_clips(clips: list):
    # write beside the file and swap, so a crash mid-write cannot leave half a clips.json
    temporary = CLIPS_PATH.with_name(CLIPS_PATH.name + ".tmp")
    temporary.write_text(json.dumps(clips, indent=2, ensure_ascii=False), encoding="utf8")
    os.replace(temporary, CLIPS_PATH)


def update_clip(name: str, changes: dict):
    clips = load_clips()
    index = next((i for i, clip in enumerate(clips) if clip["name"] == name), None)
    if index is None:
        return [f"no clip named '{name}'"]
    if not isinstance(changes, dict):
        return ["clip: must be an object"]
    # the name is the file name, so it cannot be changed here
    updated = dict(clips[index], **{key: value for key, value in changes.items() if key in CLIP_KEYS and key != "name"})
    # a crop the owner sets is the one they checked, so the "camera not checked" mark goes
    if "cam_crop" in changes and "cam_unverified" not in changes:
        updated.pop("cam_unverified", None)
    # an emptied camera crop box means "the layout's own crop"
    if updated.get("cam_crop") == "":
        del updated["cam_crop"]
    # an emptied position box means "the layout's or style's own position"
    for key in ("title_y", "caption_margin_v", "caption_font", "title_font", "review"):
        if updated.get(key) == "":
            del updated[key]
    # an emptied hook box means "no hook": the clip title is used instead
    if updated.get("hook") == "":
        del updated["hook"]
    problems = check_clip(updated)
    if not problems:
        clips[index] = updated
        save_clips(clips)
    return problems


def add_clip(clip):
    problems = check_clip(clip)
    if problems:
        return problems
    clips = load_clips()
    if any(existing["name"] == clip["name"] for existing in clips):
        problems.append(f"name: '{clip['name']}' already exists")
    # a rendered file with this name may already be posted; never write over it by accident
    if (CLIPS_DIR / clip["streamer"] / f"{clip['name']}.mp4").exists():
        problems.append(f"file: clips/{clip['streamer']}/{clip['name']}.mp4 already exists on disk")
    if not problems:
        clips.append({key: clip[key] for key in CLIP_KEYS if key in clip})
        save_clips(clips)
    return problems
