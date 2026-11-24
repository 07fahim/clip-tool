import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from peaks import pick_peaks, parse_loudness


def test_pick_peaks_keeps_the_loudest_and_spaces_them_60_seconds():
    loudness = {10: -9.0, 40: -8.0, 100: -12.0, 300: -7.0}
    assert pick_peaks(loudness, 3) == [(40, -8.0), (100, -12.0), (300, -7.0)]


def test_pick_peaks_stops_at_the_count():
    assert pick_peaks({10: -5.0, 100: -6.0, 200: -7.0}, 2) == [(10, -5.0), (100, -6.0)]


def test_parse_loudness_takes_the_loudest_reading_of_each_second():
    log = "[Parsed_ebur128_0 @ x] t: 1.1  TARGET:-23 LUFS    M: -20.5 S: -30\n" \
          "[Parsed_ebur128_0 @ x] t: 1.3  TARGET:-23 LUFS    M: -18.0 S: -30\n" \
          "[Parsed_ebur128_0 @ x] t: 2.1  TARGET:-23 LUFS    M: -25.0 S: -30\n"
    assert parse_loudness(log) == {1: -18.0, 2: -25.0}


def test_loudest_moments_raises_for_a_file_ffmpeg_cannot_read():
    from peaks import loudest_moments

    try:
        loudest_moments("definitely_not_a_real_file.m4a", 3)
    except RuntimeError as error:
        assert "loudness" in str(error)
    else:
        raise AssertionError("a bad file gave no error")


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
            print("ok", name)
