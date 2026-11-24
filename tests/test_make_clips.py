import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from make_clips import cut_cues, write_srt


def test_write_srt_writes_one_cue_per_word_group_inside_the_segments():
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "a.srt"
        write_srt(path, [(0, 10)], [(1.0, 2.0, "hello there"), (20.0, 21.0, "outside")])
        text = path.read_text(encoding="utf8")
        assert "HELLO THERE" in text and "OUTSIDE" not in text


def test_write_srt_for_a_clip_with_no_speech_is_still_a_loadable_file():
    # ffmpeg's subtitles filter cannot open an empty file, so a silent clip failed to render
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "a.srt"
        write_srt(path, [(0, 10)], [])
        text = path.read_text(encoding="utf8")
        assert text.startswith("1\n00:00:00,000 --> ") and text.strip()


def test_cut_cues_gives_the_cues_in_clip_time_inside_the_segments():
    lines = [(1.0, 2.0, "hello there"), (12.0, 13.0, "second part"), (30.0, 31.0, "outside")]
    assert cut_cues([(0, 10), (10, 20)], lines) == [(1.0, 2.0, "HELLO THERE"), (12.0, 13.0, "SECOND PART")]


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
            print("ok", name)
