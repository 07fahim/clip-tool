import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from clip_store import load_clips
from make_gta_clips import (LOCK_TRIES, MAX_WORD_SECONDS, SPEECH_WORD_LIMIT_SECONDS, censor, final_cues, group_words, load_cues,
                            make_clip, retry_while_locked)

SCRATCH = Path("scratch_new")


def test_censor_still_works():
    assert censor("Come here, nigga. Fuck it, bitch") == "Come here, n****. f**k it, b***h"


def test_continuous_speech_is_grouped_three_words_to_a_caption():
    words = [[i * 0.3, i * 0.3 + 0.25, f"w{i}"] for i in range(7)]
    assert [text for _, _, text in group_words(words)] == ["w0 w1 w2", "w3 w4 w5", "w6"]


def test_a_caption_ends_at_the_end_of_a_sentence_and_the_next_one_starts_fresh():
    words = [[0.0, 0.3, "Hello."], [0.4, 0.7, "Who"], [0.8, 1.1, "are"]]
    assert group_words(words) == [(0.0, 0.3, "Hello."), (0.4, 1.1, "Who are")]


def test_a_caption_ends_before_a_pause_so_it_is_not_on_screen_while_nobody_speaks():
    words = [[0.0, 0.3, "Wait"], [0.35, 0.6, "what"], [2.0, 2.3, "no"]]
    assert group_words(words) == [(0.0, 0.6, "Wait what"), (2.0, 2.3, "no")]


def test_a_word_whisper_stretched_into_the_silence_after_it_is_cut_short():
    # seen on real clips: one word with an end 10.6 s after its start, the caption stayed up through the silence
    words = [[0.0, 10.0, "Yo"], [10.1, 10.4, "man"]]
    assert group_words(words) == [(0.0, MAX_WORD_SECONDS, "Yo"), (10.1, 10.4, "man")]


def test_a_long_word_ends_where_the_speech_detector_says_the_speech_ends():
    # a 2.2 s word whose voice stops after 0.75 s: the rest is game sound or silence
    words = [[5.25, 7.47, "like"], [7.5, 7.8, "this"]]
    assert group_words(words, [(5.2, 6.0)]) == [(5.25, 6.1, "like"), (7.5, 7.8, "this")]


def test_a_word_the_person_really_holds_keeps_its_length():
    words = [[0.0, 2.0, "nooo"]]
    assert group_words(words, [(0.0, 2.4)]) == [(0.0, 2.0, "nooo")]


def test_even_a_word_inside_speech_never_lasts_longer_than_the_limit():
    assert group_words([[0.0, 8.0, "let's"]], [(0.0, 8.0)]) == [(0.0, SPEECH_WORD_LIMIT_SECONDS, "let's")]


def test_a_long_word_outside_every_speech_span_is_cut_to_the_short_cap():
    assert group_words([[80.0, 90.0, "here's"]], [(10.0, 20.0)]) == [(80.0, 80.0 + MAX_WORD_SECONDS, "here's")]


def test_load_cues_uses_the_speech_file_beside_the_words_file():
    import tempfile

    import make_gta_clips

    real_picks = make_gta_clips.PICKS
    with tempfile.TemporaryDirectory() as folder:
        make_gta_clips.PICKS = Path(folder)
        (Path(folder) / "x.words.json").write_text('[[5.25, 7.47, "like"]]', encoding="utf8")
        (Path(folder) / "x.speech.json").write_text("[[5.2, 6.0]]", encoding="utf8")
        try:
            assert load_cues("x") == [(5.25, 6.1, "like")]
        finally:
            make_gta_clips.PICKS = real_picks


def test_load_cues_adds_the_words_recovered_from_speech_the_first_pass_dropped():
    import tempfile

    import make_gta_clips

    real_picks = make_gta_clips.PICKS
    with tempfile.TemporaryDirectory() as folder:
        make_gta_clips.PICKS = Path(folder)
        (Path(folder) / "x.words.json").write_text('[[0.0, 0.4, "Hello."]]', encoding="utf8")
        (Path(folder) / "x.speech.json").write_text("[[0.0, 0.4], [5.0, 7.0]]", encoding="utf8")
        (Path(folder) / "x.recovered.json").write_text('[[5.1, 5.5, "Oh"], [5.6, 6.2, "my"], [6.3, 6.9, "gosh"]]', encoding="utf8")
        try:
            assert load_cues("x") == [(0.0, 0.4, "Hello."), (5.1, 6.9, "Oh my gosh")]
        finally:
            make_gta_clips.PICKS = real_picks


def test_load_cues_stars_the_swears_inside_the_new_groups():
    import tempfile

    import make_gta_clips

    real_picks = make_gta_clips.PICKS
    with tempfile.TemporaryDirectory() as folder:
        make_gta_clips.PICKS = Path(folder)
        (Path(folder) / "x.words.json").write_text('[[0.0, 0.3, "Fuck"], [0.4, 0.7, "it."], [0.8, 1.0, "Go"]]', encoding="utf8")
        (Path(folder) / "x.speech.json").write_text("[[0.0, 1.0]]", encoding="utf8")
        try:
            assert load_cues("x") == [(0.0, 0.7, "f**k it."), (0.8, 1.0, "Go")]
        finally:
            make_gta_clips.PICKS = real_picks


def test_make_clip_takes_a_clip_dict():
    clip = next(clip for clip in load_clips() if clip["name"] == "tomo_4_cash_slider")
    shutil.rmtree(SCRATCH, ignore_errors=True)
    try:
        path = make_clip(clip, SCRATCH)
        assert path == SCRATCH / "tomo" / "tomo_4_cash_slider.mp4" and path.is_file()
        seconds = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                        "default=nw=1:nk=1", str(path)], capture_output=True, text=True).stdout)
        expected = sum(end - start for start, end in clip["segments"])
        assert abs(seconds - expected) < 0.2, (seconds, expected)
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)


def test_failed_render_leaves_the_old_file_alone():
    clip = dict(next(clip for clip in load_clips() if clip["name"] == "tomo_4_cash_slider"), cam_crop="crop=9999:9999:0:0")
    shutil.rmtree(SCRATCH, ignore_errors=True)
    (SCRATCH / "tomo").mkdir(parents=True)
    old = SCRATCH / "tomo" / "tomo_4_cash_slider.mp4"
    old.write_bytes(b"already posted")
    try:
        try:
            make_clip(clip, SCRATCH)
            raise AssertionError("a failing ffmpeg run must raise")
        except subprocess.CalledProcessError:
            pass
        assert old.read_bytes() == b"already posted"
        assert not list((SCRATCH / "tomo").glob("*.tmp.mp4"))
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)


def test_render_refuses_a_hand_edited_preset_before_running_ffmpeg():
    import json
    import tempfile

    import clip_store

    clip = next(clip for clip in load_clips() if clip["name"] == "tomo_4_cash_slider")
    presets = clip_store.load_presets()
    presets["captions"][clip["caption"]]["font"] = "Arial',x='y"
    real_path = clip_store.PRESETS_PATH
    with tempfile.TemporaryDirectory() as folder:
        clip_store.PRESETS_PATH = Path(folder) / "presets.json"
        clip_store.PRESETS_PATH.write_text(json.dumps(presets), encoding="utf8")
        try:
            make_clip(clip, Path(folder) / "out")
            raise AssertionError("a bad preset must stop the render")
        except ValueError as error:
            assert "font" in str(error)
        finally:
            clip_store.PRESETS_PATH = real_path
        assert not (Path(folder) / "out").exists()


def test_render_refuses_a_hand_edited_preset_before_running_ffmpeg():
    import json
    import tempfile

    import clip_store

    clip = next(clip for clip in load_clips() if clip["name"] == "tomo_4_cash_slider")
    presets = clip_store.load_presets()
    presets["captions"][clip["caption"]]["font"] = "Arial',x='y"
    real_path = clip_store.PRESETS_PATH
    with tempfile.TemporaryDirectory() as folder:
        clip_store.PRESETS_PATH = Path(folder) / "presets.json"
        clip_store.PRESETS_PATH.write_text(json.dumps(presets), encoding="utf8")
        try:
            make_clip(clip, Path(folder) / "out")
            raise AssertionError("a bad preset must stop the render")
        except ValueError as error:
            assert "font" in str(error)
        finally:
            clip_store.PRESETS_PATH = real_path
        assert not (Path(folder) / "out").exists()


def test_load_cues_applies_the_clips_word_fixes_before_the_swear_stars():
    import tempfile

    import make_gta_clips

    real_picks = make_gta_clips.PICKS
    with tempfile.TemporaryDirectory() as folder:
        make_gta_clips.PICKS = Path(folder)
        (Path(folder) / "x.words.json").write_text('[[0.0, 0.3, "Are"], [0.4, 0.8, "bitch"], [0.9, 1.2, "Fuck"]]', encoding="utf8")
        (Path(folder) / "x.speech.json").write_text("[[0.0, 1.2]]", encoding="utf8")
        try:
            assert load_cues("x", {"bitch": "bench"}) == [(0.0, 1.2, "Are bench f**k")]
        finally:
            make_gta_clips.PICKS = real_picks


def test_clip_captions_lists_what_the_clip_will_show_in_clip_time_and_nothing_without_a_words_file():
    import tempfile

    import make_gta_clips
    from make_gta_clips import clip_captions

    real_picks = make_gta_clips.PICKS
    with tempfile.TemporaryDirectory() as folder:
        make_gta_clips.PICKS = Path(folder)
        clip = {"pick": "x", "segments": [[10, 20]], "caption_fixes": {"bitch": "bench"}}
        assert clip_captions(clip) == []
        (Path(folder) / "x.words.json").write_text('[[11.0, 11.3, "Are"], [11.4, 11.8, "bitch"], [40.0, 40.3, "outside"]]', encoding="utf8")
        (Path(folder) / "x.speech.json").write_text("[[11.0, 11.8]]", encoding="utf8")
        try:
            assert [(round(start, 2), round(end, 2), text, key) for start, end, text, key in clip_captions(clip)] == [(1.0, 1.8, "ARE BENCH", "1.00")]
        finally:
            make_gta_clips.PICKS = real_picks


def test_final_cues_apply_the_clips_timing_changes_after_cutting_to_clip_time():
    lines = [(11.0, 11.8, "Are bench")]
    clip = {"segments": [[10, 20]], "caption_timing": {"1.00": {"start": 1.3, "end": 2.4}}}
    assert final_cues(clip, lines) == [(1.3, 2.4, "ARE BENCH")]
    assert [(round(start, 2), round(end, 2), text) for start, end, text in final_cues({"segments": [[10, 20]]}, lines)] == [(1.0, 1.8, "ARE BENCH")]


def render_filter(layout: str, caption: str = "default", **changes):
    # runs make_clip with ffmpeg replaced by a stand-in that records the filter graph it was given
    import make_gta_clips

    clip = dict(next(clip for clip in load_clips() if clip["name"] == "tomo_4_cash_slider"), layout=layout, caption=caption, **changes)
    seen = {}

    def fake_run(command, **options):
        if "-filter_complex" not in command:
            # the measuring run for a gradient title box: it reports where the box sits
            return subprocess.CompletedProcess(command, 0, "", "[Parsed_bbox_1] x1:190 crop=700:84:190:94\n")
        seen["graph"] = command[command.index("-filter_complex") + 1]
        seen["command"] = command
        Path(command[-1]).write_bytes(b"video")

    real_run = make_gta_clips.subprocess.run
    make_gta_clips.subprocess.run = fake_run
    shutil.rmtree(SCRATCH, ignore_errors=True)
    try:
        make_clip(clip, SCRATCH)
    finally:
        make_gta_clips.subprocess.run = real_run
        shutil.rmtree(SCRATCH, ignore_errors=True)
    render_filter.last_command = seen["command"]
    return seen["graph"]


def test_a_split_layout_puts_the_title_where_its_title_y_says_else_on_the_seam():
    assert ":y=30," in render_filter("speed_split")
    # tomo has no title_y: the title stays on the seam, 30 px under the camera block (cam_height 620)
    assert ":y=650," in render_filter("tomo")


def test_a_clip_can_set_its_own_title_and_caption_position():
    assert ":y=400," in render_filter("tomo", title_y=400)
    assert "MarginV=200" in render_filter("tomo", caption_margin_v=200)
    # without them the layout and the caption style decide, as before
    assert ":y=650," in render_filter("tomo") and "MarginV=30" in render_filter("tomo")


def test_a_gradient_title_box_is_a_gradient_picture_under_the_title_text():
    import clip_store
    import make_gta_clips

    real = make_gta_clips.load_presets
    presets = clip_store.load_presets()
    presets["captions"]["grad"] = dict(presets["captions"]["default"], title_box="#FF512F", title_box_to="#DD2476")
    make_gta_clips.load_presets = lambda: presets
    try:
        graph = render_filter("tomo", "grad")
    finally:
        make_gta_clips.load_presets = real
    assert "gradients=s=700x84:c0=#FF512F:c1=#DD2476" in graph and "overlay=190:94" in graph
    # the text is drawn without a solid box of its own
    assert "box=1" not in graph
    assert "box=1" in render_filter("speed_split", "speed")


def test_the_render_reports_progress_to_a_file_and_the_share_done_is_read_from_it():
    import make_gta_clips

    render_filter("tomo")
    command = render_filter.last_command
    progress_file = Path(command[command.index("-progress") + 1])
    assert progress_file.name.endswith(".progress")
    folder = Path(__file__).parent / "progress_scratch"
    (folder / "tomo").mkdir(parents=True, exist_ok=True)
    clip = {"name": "x", "streamer": "tomo", "segments": [[0, 10], [20, 30]]}
    try:
        assert make_gta_clips.render_progress(clip, folder) == 0.0
        (folder / "tomo" / "x.progress").write_text("frame=1\nout_time_us=5000000\nprogress=continue\nout_time_us=10000000\nprogress=continue\n")
        assert make_gta_clips.render_progress(clip, folder) == 0.5
        (folder / "tomo" / "x.progress").write_text("out_time_us=N/A\n")
        assert make_gta_clips.render_progress(clip, folder) == 0.0
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    # the file is removed when the render ends
    assert not progress_file.exists()


def test_the_fonts_folder_is_given_to_the_captions_and_a_style_may_pick_the_title_font():
    import clip_store
    import make_gta_clips

    real = make_gta_clips.load_presets
    presets = clip_store.load_presets()
    presets["captions"]["fonty"] = dict(presets["captions"]["default"], title_font="Anton-Regular.ttf")
    make_gta_clips.load_presets = lambda: presets
    try:
        graph = render_filter("tomo", "fonty")
    finally:
        make_gta_clips.load_presets = real
    assert ":fontsdir=fonts:" in graph and "fontfile='fonts/Anton-Regular.ttf':textfile" in graph
    # a style without title_font keeps the Arial Bold title
    assert "fontfile='fonts/" not in render_filter("tomo")


def test_a_file_another_program_holds_is_tried_again_before_giving_up():
    calls = []

    def held_twice():
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("in use")
        return "done"

    assert retry_while_locked(held_twice, wait_seconds=0) == "done" and len(calls) == 3
    calls.clear()

    def held_always():
        calls.append(1)
        raise PermissionError("in use")

    try:
        retry_while_locked(held_always, wait_seconds=0)
        raise AssertionError("a lock that never opens must raise")
    except PermissionError:
        assert len(calls) == LOCK_TRIES


def test_a_caption_style_with_a_title_box_draws_the_title_in_a_highlighter_box():
    boxed = render_filter("speed", caption="speed")
    assert "box=1:boxcolor=yellow@0.95" in boxed and "fontcolor=black" in boxed
    assert "box=1" not in render_filter("tomo", caption="default")


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
            print("ok", name)
