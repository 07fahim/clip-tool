import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import clip_store
from clip_store import caption_style, check_clip, load_clips, load_presets

PICKS = Path(__file__).resolve().parent.parent / "footage" / "picks"


def make_clip(**changes):
    clip = {"name": "tomo_test", "streamer": "tomo", "pick": "tomo_lucia", "title": "A SHORT TITLE", "bottom_line": "",
            "segments": [[1, 5], [8, 12]], "caption": "default", "layout": "tomo"}
    clip.update(changes)
    return clip


def test_default_caption_matches_old_style():
    style = caption_style(load_presets()["captions"]["default"])
    assert style == ("FontName=Arial,Bold=1,FontSize=13,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                     "Outline=3,Shadow=0,Alignment=2,MarginV=30")


def test_speed_caption_is_yellow_and_higher():
    style = caption_style(load_presets()["captions"]["speed"])
    assert "PrimaryColour=&H0000FFFF" in style and "MarginV=60" in style


def test_good_clip_passes():
    assert check_clip(make_clip(), PICKS) == []


def test_name_with_path_is_rejected():
    for bad in ("../x", "a/b", "a\\b", ""):
        assert any("name" in problem for problem in check_clip(make_clip(name=bad), PICKS)), bad


def test_title_over_28_is_rejected():
    assert any("title" in problem for problem in check_clip(make_clip(title="X" * 29), PICKS))


def test_bad_segments_are_rejected():
    for bad in ([[5, 5]], [[6, 2]], [[-1, 4]], [[1, 5], [4, 9]], [[8, 12], [1, 5]], []):
        assert any("segment" in problem for problem in check_clip(make_clip(segments=bad), PICKS)), bad


def test_unknown_pick_file_is_rejected():
    assert any("pick" in problem for problem in check_clip(make_clip(pick="no_such_pick"), PICKS))


def test_unknown_preset_is_rejected():
    assert any("caption" in problem for problem in check_clip(make_clip(caption="nope"), PICKS))
    assert any("layout" in problem for problem in check_clip(make_clip(layout="nope"), PICKS))


def test_all_existing_clips_pass():
    clips = load_clips()
    # the library grows (the 15 made by hand plus clips made by the Sources tab), so only the original 15 are pinned
    assert len(clips) >= 15
    for clip in clips:
        assert check_clip(clip, PICKS) == [], clip["name"]


def test_cam_crop_override_must_be_a_crop_filter():
    assert check_clip(make_clip(cam_crop="crop=580:330:1340:470"), PICKS) == []
    for bad in ("580:330:1340:470", "crop=1:2:3", "crop=a:b:c:d", "crop=1:2:3:4,drawtext=x"):
        assert any("cam_crop" in problem for problem in check_clip(make_clip(cam_crop=bad), PICKS)), bad


def test_update_clip_changes_presets_and_refuses_bad_ones():
    import tempfile

    import clip_store

    real_path = clip_store.CLIPS_PATH
    with tempfile.TemporaryDirectory() as folder:
        clip_store.CLIPS_PATH = Path(folder) / "clips.json"
        clip_store.CLIPS_PATH.write_text(json.dumps([make_clip()]), encoding="utf8")
        try:
            assert clip_store.update_clip("tomo_test", {"caption": "speed"}) == []
            assert clip_store.load_clips()[0]["caption"] == "speed"
            assert any("caption" in problem for problem in clip_store.update_clip("tomo_test", {"caption": "nope"}))
            assert clip_store.load_clips()[0]["caption"] == "speed"
            assert any("no clip" in problem for problem in clip_store.update_clip("missing", {"caption": "speed"}))
        finally:
            clip_store.CLIPS_PATH = real_path


def test_streamer_and_pick_must_be_plain_names():
    assert any("streamer" in problem for problem in check_clip(make_clip(streamer="../../x"), PICKS))
    assert any("streamer" in problem for problem in check_clip(make_clip(streamer=3), PICKS))
    assert any("pick" in problem for problem in check_clip(make_clip(pick="../picks/tomo_lucia"), PICKS))


def test_trailing_newline_is_rejected():
    assert any("name" in problem for problem in check_clip(make_clip(name="a\n"), PICKS))
    assert any("cam_crop" in problem for problem in check_clip(make_clip(cam_crop="crop=1:2:3:4\n"), PICKS))


def test_big_at_must_be_two_ordered_numbers():
    assert check_clip(make_clip(big_at=[4, 28]), PICKS) == []
    for bad in ("x", [1], [1, 2, 3], [5, 2], ["a", "b"], [-1, 4]):
        assert any("big_at" in problem for problem in check_clip(make_clip(big_at=bad), PICKS)), bad


def test_title_y_and_caption_margin_v_are_whole_numbers_in_range():
    assert check_clip(make_clip(title_y=0, caption_margin_v=250), PICKS) == []
    for name, bad in (("title_y", 1701), ("title_y", -1), ("title_y", "x"), ("title_y", 5.5), ("caption_margin_v", 251), ("caption_margin_v", True)):
        assert any(name in problem for problem in check_clip(make_clip(**{name: bad}), PICKS)), (name, bad)


def test_wrong_types_give_problems_not_exceptions():
    junk = {"name": 5, "streamer": [], "pick": None, "title": 7, "caption": [], "layout": {}, "segments": "x",
            "bottom_line": 1}
    assert check_clip(junk, PICKS)
    assert check_clip(["not", "a", "dict"], PICKS) == ["clip: must be an object"]


def test_nan_and_infinity_segments_are_rejected():
    assert any("segment" in problem for problem in check_clip(make_clip(segments=[[float("nan"), float("inf")]]), PICKS))


def test_add_clip_keeps_only_known_keys_and_refuses_existing_file():
    import tempfile

    import clip_store

    real_path, real_dir = clip_store.CLIPS_PATH, clip_store.CLIPS_DIR
    with tempfile.TemporaryDirectory() as folder:
        clip_store.CLIPS_PATH, clip_store.CLIPS_DIR = Path(folder) / "clips.json", Path(folder)
        clip_store.CLIPS_PATH.write_text("[]", encoding="utf8")
        try:
            assert clip_store.add_clip(make_clip(evil="x")) == []
            assert "evil" not in clip_store.load_clips()[0]
            clip_store.CLIPS_PATH.write_text("[]", encoding="utf8")
            (Path(folder) / "tomo").mkdir()
            (Path(folder) / "tomo" / "tomo_test.mp4").write_bytes(b"posted")
            assert any("already exists on disk" in problem for problem in clip_store.add_clip(make_clip()))
        finally:
            clip_store.CLIPS_PATH, clip_store.CLIPS_DIR = real_path, real_dir


def test_add_clip_saves_good_and_refuses_duplicate():
    import tempfile

    import clip_store

    real_path = clip_store.CLIPS_PATH
    with tempfile.TemporaryDirectory() as folder:
        clip_store.CLIPS_PATH = Path(folder) / "clips.json"
        clip_store.CLIPS_PATH.write_text("[]", encoding="utf8")
        try:
            assert clip_store.add_clip(make_clip()) == []
            assert [clip["name"] for clip in clip_store.load_clips()] == ["tomo_test"]
            assert any("already exists" in problem for problem in clip_store.add_clip(make_clip()))
            assert len(clip_store.load_clips()) == 1
        finally:
            clip_store.CLIPS_PATH = real_path


TEXT_PRESETS = {"creators": {"tomo": {"youtube": ["gta6", "#gaming"], "facebook": ["#gta6", "fyp"]}}}


def test_build_text_youtube():
    clip = make_clip(hook="Lucia cooked him", body="Who wins the rematch?")
    text = clip_store.build_text(clip, "youtube", TEXT_PRESETS)
    assert text == {"title": "Lucia cooked him", "description": "Who wins the rematch? #gta6 #gaming #shorts",
                    "used_fallback": False}


def test_build_text_facebook():
    clip = make_clip(hook="Lucia cooked him", body="Who wins the rematch?")
    text = clip_store.build_text(clip, "facebook", TEXT_PRESETS)
    assert text == {"caption": "Lucia cooked him\nWho wins the rematch?\n#gta6 #fyp", "used_fallback": False}


def test_tags_deduplicated_and_hashed():
    clip = make_clip(hook="H", extra_tags=["GTA6", "#gta6", "Money"])
    description = clip_store.build_text(clip, "youtube", TEXT_PRESETS)["description"]
    assert description == "#gta6 #gaming #Money #shorts"


def test_youtube_tag_cap_is_15_with_shorts_last_and_extras_kept():
    presets = {"creators": {"tomo": {"youtube": [f"s{number}" for number in range(20)]}}}
    clip = make_clip(hook="H", extra_tags=["mine1", "mine2"])
    tags = clip_store.build_text(clip, "youtube", presets)["description"].split()
    assert len(tags) == 15 and tags[-1] == "#shorts"
    assert "#mine1" in tags and "#mine2" in tags and "#s0" in tags and "#s12" not in tags


def test_facebook_has_no_shorts_tag():
    assert "#shorts" not in clip_store.build_text(make_clip(hook="H"), "facebook", TEXT_PRESETS)["caption"]


def test_emptied_position_boxes_remove_the_clips_own_positions():
    import tempfile

    real_path = clip_store.CLIPS_PATH
    with tempfile.TemporaryDirectory() as folder:
        clip_store.CLIPS_PATH = Path(folder) / "clips.json"
        clip_store.CLIPS_PATH.write_text(json.dumps([make_clip(title_y=400, caption_margin_v=200)]), encoding="utf8")
        try:
            assert clip_store.update_clip("tomo_test", {"title_y": "", "caption_margin_v": ""}) == []
            saved = clip_store.load_clips()[0]
            assert "title_y" not in saved and "caption_margin_v" not in saved
        finally:
            clip_store.CLIPS_PATH = real_path


def test_missing_hook_falls_back_to_title_and_says_so():
    text = clip_store.build_text(make_clip(title="TOMO PLAYS AS LUCIA"), "youtube", TEXT_PRESETS)
    # sentence case would turn GTA and names into "Gta", so the title is kept as written
    assert text["title"] == "TOMO PLAYS AS LUCIA" and text["used_fallback"] is True


def test_empty_hook_update_removes_the_hook():
    import tempfile

    real_path = clip_store.CLIPS_PATH
    with tempfile.TemporaryDirectory() as folder:
        clip_store.CLIPS_PATH = Path(folder) / "clips.json"
        clip_store.CLIPS_PATH.write_text(json.dumps([make_clip(hook="Old hook")]), encoding="utf8")
        try:
            assert clip_store.update_clip("tomo_test", {"hook": ""}) == []
            assert "hook" not in clip_store.load_clips()[0]
            assert any("hook" in problem for problem in clip_store.update_clip("tomo_test", {"hook": "X" * 101}))
        finally:
            clip_store.CLIPS_PATH = real_path


def test_unknown_platform_raises():
    try:
        clip_store.build_text(make_clip(), "tiktok", TEXT_PRESETS)
    except ValueError:
        return
    raise AssertionError("no ValueError")


def test_hook_over_100_is_refused():
    assert check_clip(make_clip(hook="X" * 100), PICKS) == []
    assert any("hook" in problem for problem in check_clip(make_clip(hook="X" * 101), PICKS))
    assert any("hook" in problem for problem in check_clip(make_clip(hook=""), PICKS))


def test_hook_with_only_spaces_or_a_line_break_is_refused():
    for bad in ("   ", " padded ", "two\nlines", "tab\there"):
        assert any("hook" in problem for problem in check_clip(make_clip(hook=bad), PICKS)), repr(bad)


def test_more_than_14_extra_tags_are_refused():
    assert check_clip(make_clip(extra_tags=[f"t{number}" for number in range(14)]), PICKS) == []
    assert any("extra_tags" in problem for problem in check_clip(make_clip(extra_tags=[f"t{n}" for n in range(15)]), PICKS))


def test_hook_body_and_tag_wrong_types_give_problems():
    for field, bad in (("hook", 5), ("body", []), ("extra_tags", "x"), ("extra_tags", [1]), ("extra_tags", ["a b"]),
                       ("extra_tags", ["#"])):
        assert any(field in problem for problem in check_clip(make_clip(**{field: bad}), PICKS)), (field, bad)


def test_hook_body_extra_tags_are_kept_by_add_clip():
    import tempfile

    real_path = clip_store.CLIPS_PATH
    with tempfile.TemporaryDirectory() as folder:
        clip_store.CLIPS_PATH = Path(folder) / "clips.json"
        clip_store.CLIPS_PATH.write_text("[]", encoding="utf8")
        try:
            assert clip_store.add_clip(make_clip(hook="H", body="B", extra_tags=["x"])) == []
            saved = clip_store.load_clips()[0]
            assert (saved["hook"], saved["body"], saved["extra_tags"]) == ("H", "B", ["x"])
        finally:
            clip_store.CLIPS_PATH = real_path


def test_existing_clips_have_hooks_and_build_text_for_both_platforms():
    presets = load_presets()
    for clip in load_clips():
        assert clip.get("hook"), clip["name"]
        youtube = clip_store.build_text(clip, "youtube", presets)
        assert not youtube["used_fallback"] and youtube["description"].endswith("#shorts"), clip["name"]
        assert len([word for word in youtube["description"].split() if word.startswith("#")]) <= 15, clip["name"]
        assert clip_store.build_text(clip, "facebook", presets)["caption"].startswith(clip["hook"]), clip["name"]


GOOD_CAPTION = {"font": "Arial", "size": 13, "primary": "&H00FFFFFF", "outline_colour": "&H00000000", "outline": 3,
                "margin_v": 30, "title_colour": "white", "bottom_colour": "#FFD400"}
GOOD_SPLIT = {"type": "split", "cam_crop": "crop=580:330:1340:470", "cam_height": 620, "game_crop": "crop=897:1080:440:0"}


def test_good_caption_preset_passes_and_bad_values_are_refused():
    assert clip_store.check_caption_preset(GOOD_CAPTION) == []
    # the values go into the ffmpeg style string and filter, so nothing that could add options gets through
    for field, bad in (("font", "Arial,Outline=9"), ("font", ""), ("size", "13"), ("size", 0), ("size", 500),
                       ("primary", "white"), ("primary", "&H00FFFFFF,Shadow=9"), ("outline", -1), ("margin_v", 9999),
                       ("title_colour", "white:x=1"), ("bottom_colour", "red;"), ("size", True)):
        assert any(field in problem for problem in clip_store.check_caption_preset(dict(GOOD_CAPTION, **{field: bad}))), (
            field, bad)
    missing = {key: value for key, value in GOOD_CAPTION.items() if key != "size"}
    assert any("size" in problem for problem in clip_store.check_caption_preset(missing))
    assert clip_store.check_caption_preset("x") == ["caption style: must be an object"]


def test_layout_preset_needs_the_fields_of_its_type():
    assert clip_store.check_layout_preset(GOOD_SPLIT) == []
    assert clip_store.check_layout_preset({"type": "face", "cam_crop": "crop=608:1080:600:0"}) == []
    fullframe = dict(GOOD_SPLIT, type="fullframe", big_width=820)
    assert clip_store.check_layout_preset(fullframe) == []
    assert any("big_width" in problem for problem in clip_store.check_layout_preset(dict(GOOD_SPLIT, type="fullframe")))
    assert any("cam_height" in problem for problem in clip_store.check_layout_preset(
        {key: value for key, value in GOOD_SPLIT.items() if key != "cam_height"}))
    assert any("type" in problem for problem in clip_store.check_layout_preset(dict(GOOD_SPLIT, type="spin")))
    for field, bad in (("cam_crop", "crop=1:2:3:4,drawtext=x"), ("game_crop", "crop=a:b:c:d"), ("cam_height", 5),
                       ("cam_height", 1800), ("cam_height", "620")):
        assert any(field in problem for problem in clip_store.check_layout_preset(dict(GOOD_SPLIT, **{field: bad}))), (
            field, bad)


def test_save_preset_adds_edits_and_drops_unknown_keys():
    import tempfile

    real_path = clip_store.PRESETS_PATH
    with tempfile.TemporaryDirectory() as folder:
        clip_store.PRESETS_PATH = Path(folder) / "presets.json"
        clip_store.PRESETS_PATH.write_text(json.dumps({"captions": {}, "layouts": {}}), encoding="utf8")
        try:
            assert clip_store.save_preset("captions", "loud", dict(GOOD_CAPTION, evil="x")) == []
            assert "evil" not in load_presets()["captions"]["loud"]
            assert clip_store.save_preset("captions", "loud", dict(GOOD_CAPTION, size=20)) == []
            assert load_presets()["captions"]["loud"]["size"] == 20
            assert clip_store.save_preset("layouts", "wide", GOOD_SPLIT) == []
            assert load_presets()["layouts"]["wide"]["cam_height"] == 620
            for kind, name, preset in (("fonts", "x", GOOD_CAPTION), ("captions", "../x", GOOD_CAPTION),
                                       ("captions", "", GOOD_CAPTION), ("captions", "x", dict(GOOD_CAPTION, size=0)),
                                       ("layouts", "y", {"type": "spin"})):
                assert clip_store.save_preset(kind, name, preset), (kind, name)
            assert set(load_presets()["captions"]) == {"loud"} and set(load_presets()["layouts"]) == {"wide"}
        finally:
            clip_store.PRESETS_PATH = real_path


GOOD_CREATOR = {"youtube": ["gta6", "#gaming"], "facebook": ["fyp"], "caption": "default", "layout": "tomo"}


def test_good_creator_passes_and_bad_values_are_refused():
    presets = load_presets()
    assert clip_store.check_creator(GOOD_CREATOR, presets) == []
    for field, bad in (("youtube", "gta6"), ("youtube", ["a b"]), ("youtube", [5]), ("facebook", None),
                       ("youtube", [f"t{number}" for number in range(31)]), ("caption", "nope"), ("caption", 3),
                       ("layout", "nope")):
        assert any(field in problem for problem in clip_store.check_creator(dict(GOOD_CREATOR, **{field: bad}), presets)), (
            field, bad)
    assert clip_store.check_creator("x", presets) == ["creator: must be an object"]
    assert any("layout" in problem for problem in clip_store.check_creator({"youtube": [], "facebook": [], "caption": "default"}, presets))


def test_save_creator_adds_edits_and_refuses_bad_ones():
    import tempfile

    real_path = clip_store.PRESETS_PATH
    with tempfile.TemporaryDirectory() as folder:
        clip_store.PRESETS_PATH = Path(folder) / "presets.json"
        clip_store.PRESETS_PATH.write_text(real_path.read_text(encoding="utf8"), encoding="utf8")
        try:
            assert clip_store.save_creator("mrbeast", dict(GOOD_CREATOR, evil="x")) == []
            saved = load_presets()["creators"]["mrbeast"]
            assert "evil" not in saved and saved["youtube"] == ["gta6", "#gaming"]
            assert clip_store.save_creator("mrbeast", dict(GOOD_CREATOR, youtube=["new"])) == []
            assert load_presets()["creators"]["mrbeast"]["youtube"] == ["new"]
            for name, creator in (("../x", GOOD_CREATOR), ("", GOOD_CREATOR), ("x", dict(GOOD_CREATOR, layout="nope"))):
                assert clip_store.save_creator(name, creator), name
            assert "x" not in load_presets()["creators"]
            assert "tomo" in load_presets()["creators"]
        finally:
            clip_store.PRESETS_PATH = real_path


def test_every_clip_streamer_is_a_creator_with_a_valid_look():
    presets = load_presets()
    assert {clip["streamer"] for clip in load_clips()} <= set(presets["creators"])
    for name, creator in presets["creators"].items():
        assert clip_store.check_creator(creator, presets) == [], name


def test_own_tags_only_leaves_out_the_creators_game_tags():
    presets = load_presets()
    clip = make_clip(streamer="speed", hook="Speed spotted a typo", extra_tags=["ishowspeed", "fortnite"], own_tags_only=True)
    youtube = clip_store.build_text(clip, "youtube", presets)
    assert "#gta6" not in youtube["description"] and "#fortnite" in youtube["description"]
    assert youtube["description"].endswith("#shorts")
    assert "#gta6" not in clip_store.build_text(clip, "facebook", presets)["caption"]


def test_a_clip_without_the_flag_still_gets_the_creators_tags():
    presets = load_presets()
    clip = make_clip(streamer="speed", hook="Speed in GTA", extra_tags=["fortnite"])
    assert "#gta6" in clip_store.build_text(clip, "youtube", presets)["description"]


def test_own_tags_only_must_be_true_or_false():
    assert any("own_tags_only" in problem for problem in check_clip(make_clip(own_tags_only="yes"), PICKS))
    assert not any("own_tags_only" in problem for problem in check_clip(make_clip(own_tags_only=True), PICKS))


def test_cam_unverified_is_kept_checked_and_cleared_by_a_new_crop_or_by_the_owner():
    import tempfile

    assert any("cam_unverified" in problem for problem in check_clip(make_clip(cam_unverified="yes"), PICKS))
    real_path = clip_store.CLIPS_PATH
    with tempfile.TemporaryDirectory() as folder:
        clip_store.CLIPS_PATH = Path(folder) / "clips.json"
        clip_store.CLIPS_PATH.write_text(json.dumps([make_clip(cam_unverified=True)]), encoding="utf8")
        try:
            assert clip_store.load_clips()[0]["cam_unverified"] is True
            assert clip_store.update_clip("tomo_test", {"hook": "New"}) == []
            assert clip_store.load_clips()[0]["cam_unverified"] is True
            assert clip_store.update_clip("tomo_test", {"cam_crop": "crop=300:200:10:20"}) == []
            assert "cam_unverified" not in clip_store.load_clips()[0] or clip_store.load_clips()[0]["cam_unverified"] is False
            clip_store.CLIPS_PATH.write_text(json.dumps([make_clip(cam_unverified=True)]), encoding="utf8")
            assert clip_store.update_clip("tomo_test", {"cam_unverified": False}) == []
            assert clip_store.load_clips()[0]["cam_unverified"] is False
        finally:
            clip_store.CLIPS_PATH = real_path


def test_caption_fixes_must_be_plain_words_and_are_kept_by_update():
    import tempfile

    assert check_clip(make_clip(caption_fixes={"bitch": "bench", "uh": ""}), PICKS) == []
    for bad in ({"a b": "x"}, {"x": "a{\b}"}, {"": "x"}, ["bitch"], {"x": 5}, {f"w{i}": "x" for i in range(30)}):
        assert any("caption_fixes" in problem for problem in check_clip(make_clip(caption_fixes=bad), PICKS)), bad
    real_path = clip_store.CLIPS_PATH
    with tempfile.TemporaryDirectory() as folder:
        clip_store.CLIPS_PATH = Path(folder) / "clips.json"
        clip_store.CLIPS_PATH.write_text(json.dumps([make_clip()]), encoding="utf8")
        try:
            assert clip_store.update_clip("tomo_test", {"caption_fixes": {"bitch": "bench"}}) == []
            assert clip_store.load_clips()[0]["caption_fixes"] == {"bitch": "bench"}
        finally:
            clip_store.CLIPS_PATH = real_path


def test_caption_timing_must_be_a_start_before_an_end_for_a_cue_key():
    assert check_clip(make_clip(caption_timing={"1.31": {"start": 1.0, "end": 2.5}}), PICKS) == []
    for bad in ({"x": {"start": 1, "end": 2}}, {"1.00": {"start": 2, "end": 1}}, {"1.00": {"start": 1}}, {"1.00": {"start": -1, "end": 2}},
                {"1.00": [1, 2]}, ["1.00"], {f"{i}.00": {"start": 1, "end": 2} for i in range(101)}, {"1.00": {"start": 1, "end": "2"}}):
        assert any("caption_timing" in problem for problem in check_clip(make_clip(caption_timing=bad), PICKS)), bad


def test_an_emptied_camera_crop_removes_the_clips_own_crop_and_a_bad_one_is_refused():
    import tempfile

    real_path = clip_store.CLIPS_PATH
    with tempfile.TemporaryDirectory() as folder:
        clip_store.CLIPS_PATH = Path(folder) / "clips.json"
        clip_store.CLIPS_PATH.write_text(json.dumps([make_clip(cam_crop="crop=388:240:0:461")]), encoding="utf8")
        try:
            assert any("cam_crop" in problem for problem in clip_store.update_clip("tomo_test", {"cam_crop": "crop=1:2"}))
            assert clip_store.update_clip("tomo_test", {"cam_crop": ""}) == []
            assert "cam_crop" not in clip_store.load_clips()[0]
        finally:
            clip_store.CLIPS_PATH = real_path


def test_a_split_layout_may_set_where_the_title_sits():
    assert clip_store.check_layout_preset(dict(GOOD_SPLIT, title_y=30)) == []
    for bad in (-1, 1701, "x", True, 3.5):
        assert any("title_y" in problem for problem in clip_store.check_layout_preset(dict(GOOD_SPLIT, title_y=bad))), bad


def test_a_saved_split_layout_keeps_its_title_position():
    import tempfile

    real_path = clip_store.PRESETS_PATH
    with tempfile.TemporaryDirectory() as folder:
        clip_store.PRESETS_PATH = Path(folder) / "presets.json"
        clip_store.PRESETS_PATH.write_text(json.dumps({"captions": {}, "layouts": {}}), encoding="utf8")
        try:
            assert clip_store.save_preset("layouts", "top", dict(GOOD_SPLIT, title_y=30)) == []
            assert load_presets()["layouts"]["top"]["title_y"] == 30
            assert clip_store.save_preset("layouts", "plain", GOOD_SPLIT) == []
            assert "title_y" not in load_presets()["layouts"]["plain"]
        finally:
            clip_store.PRESETS_PATH = real_path


def test_saving_a_layout_from_the_page_form_keeps_the_stored_title_position():
    import tempfile

    real_path = clip_store.PRESETS_PATH
    with tempfile.TemporaryDirectory() as folder:
        clip_store.PRESETS_PATH = Path(folder) / "presets.json"
        clip_store.PRESETS_PATH.write_text(json.dumps({"captions": {}, "layouts": {}}), encoding="utf8")
        try:
            assert clip_store.save_preset("layouts", "top", dict(GOOD_SPLIT, title_y=30)) == []
            # the page's layout form has no title_y box, so it sends the preset without one
            assert clip_store.save_preset("layouts", "top", dict(GOOD_SPLIT, cam_height=640)) == []
            saved = load_presets()["layouts"]["top"]
            assert saved["cam_height"] == 640 and saved["title_y"] == 30
            assert clip_store.save_preset("layouts", "plain", GOOD_SPLIT) == []
            assert "title_y" not in load_presets()["layouts"]["plain"]
        finally:
            clip_store.PRESETS_PATH = real_path


def test_a_creator_may_carry_its_own_bottom_line_and_keeps_it_when_saved_without_one():
    import tempfile

    presets = load_presets()
    assert clip_store.check_creator(dict(GOOD_CREATOR, bottom_line="follow for daily GTA 6 clips"), presets) == []
    for bad in (5, "x" * 41, "two\nlines"):
        assert any("bottom_line" in problem for problem in clip_store.check_creator(dict(GOOD_CREATOR, bottom_line=bad), presets)), bad
    real_path = clip_store.PRESETS_PATH
    with tempfile.TemporaryDirectory() as folder:
        clip_store.PRESETS_PATH = Path(folder) / "presets.json"
        clip_store.PRESETS_PATH.write_text(real_path.read_text(encoding="utf8"), encoding="utf8")
        try:
            assert clip_store.save_creator("loud", dict(GOOD_CREATOR, bottom_line="follow me")) == []
            assert load_presets()["creators"]["loud"]["bottom_line"] == "follow me"
            # the page's form does not send it, and saving the form must not wipe it
            assert clip_store.save_creator("loud", dict(GOOD_CREATOR, youtube=["new"])) == []
            assert load_presets()["creators"]["loud"]["bottom_line"] == "follow me"
        finally:
            clip_store.PRESETS_PATH = real_path


def test_speed_has_a_follow_line_and_a_split_layout_with_the_title_on_top():
    presets = load_presets()
    assert presets["creators"]["speed"]["bottom_line"] == "follow for daily GTA 6 clips"
    split = presets["layouts"]["speed_split"]
    assert split["type"] == "split" and split["title_y"] < split["cam_height"]
    assert clip_store.check_layout_preset(split) == []


def test_a_caption_style_may_put_its_title_in_a_highlighter_box():
    assert clip_store.check_caption_preset(dict(GOOD_CAPTION, title_box="yellow")) == []
    assert clip_store.check_caption_preset(dict(GOOD_CAPTION, title_box="#FFD400")) == []
    for bad in (5, "yellow:x=1", "", None):
        assert any("title_box" in problem for problem in clip_store.check_caption_preset(dict(GOOD_CAPTION, title_box=bad))), bad
    assert clip_store.check_caption_preset(GOOD_CAPTION) == []


def test_saving_a_caption_style_from_the_page_form_keeps_its_highlighter_box():
    import tempfile

    real_path = clip_store.PRESETS_PATH
    with tempfile.TemporaryDirectory() as folder:
        clip_store.PRESETS_PATH = Path(folder) / "presets.json"
        clip_store.PRESETS_PATH.write_text(json.dumps({"captions": {}, "layouts": {}}), encoding="utf8")
        try:
            assert clip_store.save_preset("captions", "loud", dict(GOOD_CAPTION, title_box="yellow")) == []
            assert load_presets()["captions"]["loud"]["title_box"] == "yellow"
            # the page's caption form has no box for it, so it sends the style without one
            assert clip_store.save_preset("captions", "loud", dict(GOOD_CAPTION, size=20)) == []
            assert load_presets()["captions"]["loud"]["size"] == 20 and load_presets()["captions"]["loud"]["title_box"] == "yellow"
            assert clip_store.save_preset("captions", "plain", GOOD_CAPTION) == []
            assert "title_box" not in load_presets()["captions"]["plain"]
        finally:
            clip_store.PRESETS_PATH = real_path


def test_speed_titles_are_black_text_in_a_yellow_highlighter_box():
    speed = load_presets()["captions"]["speed"]
    assert speed["title_box"] == "yellow" and speed["title_colour"] == "black"
    assert "title_box" not in load_presets()["captions"]["default"]


def test_a_title_box_can_have_a_second_colour_for_a_gradient():
    assert clip_store.check_caption_preset(dict(GOOD_CAPTION, title_box="#FF512F", title_box_to="#DD2476")) == []
    # a gradient needs the box it fills, and a colour the filter can read
    assert any("title_box_to" in problem for problem in clip_store.check_caption_preset(dict(GOOD_CAPTION, title_box_to="#DD2476")))
    assert any("title_box_to" in problem for problem in clip_store.check_caption_preset(dict(GOOD_CAPTION, title_box="red", title_box_to="x;y")))


def test_saving_a_style_with_empty_box_colours_removes_the_stored_ones():
    import tempfile

    real_path = clip_store.PRESETS_PATH
    with tempfile.TemporaryDirectory() as folder:
        clip_store.PRESETS_PATH = Path(folder) / "presets.json"
        clip_store.PRESETS_PATH.write_text(json.dumps({"captions": {}, "layouts": {}}), encoding="utf8")
        try:
            assert clip_store.save_preset("captions", "g", dict(GOOD_CAPTION, title_box="red", title_box_to="blue")) == []
            assert clip_store.save_preset("captions", "g", dict(GOOD_CAPTION, title_box="red", title_box_to="")) == []
            assert "title_box_to" not in load_presets()["captions"]["g"] and load_presets()["captions"]["g"]["title_box"] == "red"
            assert clip_store.save_preset("captions", "g", dict(GOOD_CAPTION, title_box="")) == []
            assert "title_box" not in load_presets()["captions"]["g"]
        finally:
            clip_store.PRESETS_PATH = real_path


def test_a_caption_style_may_name_a_title_font_file_from_the_fonts_folder():
    assert clip_store.check_caption_preset(dict(GOOD_CAPTION, title_font="Anton-Regular.ttf")) == []
    for bad in ("../x.ttf", "a/b.ttf", "Anton.exe", "", 5, "x;y.ttf"):
        assert any("title_font" in problem for problem in clip_store.check_caption_preset(dict(GOOD_CAPTION, title_font=bad))), bad


def test_a_clip_may_carry_its_own_caption_font_and_title_font():
    assert check_clip(make_clip(caption_font="Anton", title_font="Anton-Regular.ttf"), PICKS) == []
    for name, bad in (("caption_font", "A;b"), ("caption_font", 5), ("title_font", "../x.ttf"), ("title_font", "x.exe")):
        assert any(name in problem for problem in check_clip(make_clip(**{name: bad}), PICKS)), (name, bad)


def test_emptied_font_boxes_remove_the_clips_own_fonts():
    import tempfile

    real_path = clip_store.CLIPS_PATH
    with tempfile.TemporaryDirectory() as folder:
        clip_store.CLIPS_PATH = Path(folder) / "clips.json"
        clip_store.CLIPS_PATH.write_text(json.dumps([make_clip(caption_font="Anton", title_font="Anton-Regular.ttf")]), encoding="utf8")
        try:
            assert clip_store.update_clip("tomo_test", {"caption_font": "", "title_font": ""}) == []
            saved = clip_store.load_clips()[0]
            assert "caption_font" not in saved and "title_font" not in saved
        finally:
            clip_store.CLIPS_PATH = real_path


def test_a_clip_review_is_approved_or_rejected_and_an_empty_value_clears_it():
    import tempfile

    assert check_clip(make_clip(review="approved"), PICKS) == [] and check_clip(make_clip(review="rejected"), PICKS) == []
    for bad in ("maybe", "", 5, None):
        assert any("review" in problem for problem in check_clip(make_clip(review=bad), PICKS)), bad
    real_path = clip_store.CLIPS_PATH
    with tempfile.TemporaryDirectory() as folder:
        clip_store.CLIPS_PATH = Path(folder) / "clips.json"
        clip_store.CLIPS_PATH.write_text(json.dumps([make_clip()]), encoding="utf8")
        try:
            assert clip_store.update_clip("tomo_test", {"review": "approved"}) == []
            assert clip_store.load_clips()[0]["review"] == "approved"
            assert clip_store.update_clip("tomo_test", {"review": ""}) == []
            assert "review" not in clip_store.load_clips()[0]
        finally:
            clip_store.CLIPS_PATH = real_path


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
            print("ok", name)
