import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import detect
from detect import RateLimited, build_candidates, load_comment_moments, rank_candidates, run_command, scan_source
from sources import add_source, load_sources

GOOD_URL = "https://www.youtube.com/watch?v=UeHMwf9vIss"
WINDOWS = [{"offset": 40, "lines": [(45, "oh no"), (60, "what was that")]}, {"offset": 340, "lines": []}]


def test_build_candidates_pairs_each_peak_with_its_window_in_time_order():
    peaks = [(100, -8.0), (400, -7.5)]
    assert build_candidates(WINDOWS, peaks) == [
        {"id": "c40", "peak": 100, "loudness": -8.0, "start": 40, "end": 130, "text": "oh no what was that", "words": 5},
        {"id": "c340", "peak": 400, "loudness": -7.5, "start": 340, "end": 430, "text": "", "words": 0},
    ]


def test_build_candidates_drops_lines_that_are_only_the_whisper_name_hint():
    windows = [{"offset": 0, "lines": [(3, "IShowSpeed. Kai Cenat. Tomo. Lucia. Jason."), (9, "Oh my gosh Jason"), (12, "Kai Cenat."),
                                       (15, "Lucia, Jason.")]}]
    assert build_candidates(windows, [(20, -9.0)])[0]["text"] == "Oh my gosh Jason"


def test_build_candidates_starts_the_window_at_zero_for_an_early_peak():
    assert build_candidates([{"offset": 0, "lines": [(3, "hi")]}], [(20, -9.0)])[0]["start"] == 0


class FakeRunner:
    def __init__(self):
        self.commands = []

    def __call__(self, command: list, env=None):
        self.commands.append(command)
        if "-o" in command:
            Path(command[command.index("-o") + 1]).write_bytes(b"audio")
        elif command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"wav")
        else:
            Path(command[-1]).write_text("=== 0:00:40\n0:00:45 oh no\n=== 0:05:40\n", encoding="utf8")


def make_source(root: Path) -> dict:
    return add_source(GOOD_URL, "speed", fetch=lambda url: {"title": "t", "duration": 600}, root=root)


def test_scan_source_downloads_cuts_windows_transcribes_and_saves_candidates():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source = make_source(root)
        runner = FakeRunner()
        candidates = scan_source(source["id"], root=root, runner=runner, find_peaks=lambda audio, count: [(100, -8.0), (400, -7.5)])
        kinds = [command[0] for command in runner.commands]
        assert kinds.count("ffmpeg") == 2 and len(runner.commands) == 4
        assert candidates[0]["text"] == "oh no" and len(candidates) == 2
        saved = json.loads((root / source["id"] / "candidates.json").read_text(encoding="utf8"))
        assert saved == candidates
        assert load_sources(root)[0]["status"] == "scanned"


def test_a_second_scan_reuses_the_audio_and_the_finished_transcript():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source = make_source(root)
        peaks = lambda audio, count: [(100, -8.0), (400, -7.5)]
        scan_source(source["id"], root=root, runner=FakeRunner(), find_peaks=peaks)
        again = FakeRunner()
        scan_source(source["id"], root=root, runner=again, find_peaks=peaks)
        assert again.commands == []


def test_a_failed_download_leaves_no_audio_file_and_the_source_not_scanned():
    def failing(command: list, env=None):
        Path(command[command.index("-o") + 1]).write_bytes(b"half")
        raise RuntimeError("network")

    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source = make_source(root)
        try:
            scan_source(source["id"], root=root, runner=failing, find_peaks=lambda audio, count: [])
        except RuntimeError:
            pass
        assert not (root / source["id"] / "audio.m4a").exists()
        assert load_sources(root)[0]["status"] == "failed"


def test_build_candidates_ends_a_late_window_at_the_end_of_the_video():
    candidates = build_candidates([{"offset": 540, "lines": []}], [(600, -9.0)], duration=610)
    assert candidates[0]["end"] == 610


def test_a_stopped_whisper_run_is_run_again_not_trusted():
    class StoppingRunner(FakeRunner):
        def __call__(self, command: list, env=None):
            if command[0] != "ffmpeg" and "-o" not in command:
                Path(command[-1]).write_text("=== 0:00:40\n0:00:45 first window only\n", encoding="utf8")
                raise RuntimeError("PC went off")
            return super().__call__(command, env)

    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source = make_source(root)
        peaks = lambda audio, count: [(100, -8.0), (400, -7.5)]
        try:
            scan_source(source["id"], root=root, runner=StoppingRunner(), find_peaks=peaks)
        except RuntimeError:
            pass
        assert not (root / source["id"] / "lines.txt").exists()
        again = FakeRunner()
        candidates = scan_source(source["id"], root=root, runner=again, find_peaks=peaks)
        assert any(command[0] != "ffmpeg" and "-o" not in command for command in again.commands)
        assert candidates[0]["text"] == "oh no"


def test_a_half_cut_window_is_not_kept():
    class CuttingFails(FakeRunner):
        def __call__(self, command: list, env=None):
            if command[0] == "ffmpeg":
                Path(command[-1]).write_bytes(b"half")
                raise RuntimeError("PC went off")
            return super().__call__(command, env)

    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source = make_source(root)
        try:
            scan_source(source["id"], root=root, runner=CuttingFails(), find_peaks=lambda audio, count: [(100, -8.0)])
        except RuntimeError:
            pass
        assert list((root / source["id"] / "win").glob("*.wav")) == []


def test_run_command_times_out_a_hung_program():
    import subprocess

    from detect import run_command

    try:
        run_command([sys.executable, "-c", "import time; time.sleep(5)"], timeout=1)
    except subprocess.TimeoutExpired:
        pass
    else:
        raise AssertionError("a hung program was waited for")


def test_rank_candidates_puts_real_speech_first_then_the_louder_moment():
    candidates = [{"id": "a", "words": 0, "loudness": -5.0}, {"id": "b", "words": 12, "loudness": -20.0},
                  {"id": "c", "words": 2, "loudness": -8.0}, {"id": "d", "words": 9, "loudness": -12.0}]
    ranked = rank_candidates(candidates)
    assert {candidate["id"]: candidate["rank"] for candidate in ranked} == {"d": 1, "b": 2, "a": 3, "c": 4}
    assert [candidate["id"] for candidate in ranked] == ["a", "b", "c", "d"]


def test_rank_candidates_puts_a_moment_viewers_liked_above_a_louder_one_but_after_silence():
    candidates = [{"id": "loud", "words": 12, "loudness": -5.0, "comment_likes": 0},
                  {"id": "liked", "words": 12, "loudness": -20.0, "comment_likes": 30},
                  {"id": "liked_silent", "words": 0, "loudness": -5.0, "comment_likes": 99}]
    ranked = rank_candidates(candidates)
    assert {candidate["id"]: candidate["rank"] for candidate in ranked} == {"liked": 1, "loud": 2, "liked_silent": 3}


def write_moments(root: Path, source: dict, moments: list) -> None:
    (root / source["id"] / "comment_moments.json").write_text(json.dumps(moments), encoding="utf8")


def test_load_comment_moments_reads_the_file_and_drops_bad_entries():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source = make_source(root)
        assert load_comment_moments(root / source["id"], 600) == []
        write_moments(root, source, [{"second": 120, "likes": 31, "text": "ok"}, {"second": 9000, "likes": 5, "text": "past the end"},
                                     {"second": "7", "likes": 5, "text": "not a number"}, {"likes": 4}])
        assert load_comment_moments(root / source["id"], 600) == [{"second": 120, "likes": 31, "text": "ok"}]


def test_scan_marks_a_peak_a_comment_points_at_and_adds_a_candidate_for_one_far_from_every_peak():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source = make_source(root)
        write_moments(root, source, [{"second": 120, "likes": 31, "text": "the clip always scream"},
                                     {"second": 450, "likes": 29, "text": "the nicest kid"}])
        candidates = scan_source(source["id"], root=root, runner=FakeRunner(), find_peaks=lambda audio, count: [(100, -8.0)])
        by_id = {candidate["id"]: candidate for candidate in candidates}
        assert by_id["c40"]["comment_likes"] == 31 and by_id["c40"]["comment"] == "the clip always scream"
        assert by_id["c390"]["comment_likes"] == 29 and by_id["c390"]["peak"] == 450 and by_id["c390"]["loudness"] < -60
        assert len(candidates) == 2


def test_a_rescan_after_comment_moments_arrive_cuts_and_transcribes_the_new_window():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source = make_source(root)
        peaks = lambda audio, count: [(100, -8.0)]
        scan_source(source["id"], root=root, runner=FakeRunner(), find_peaks=peaks)
        write_moments(root, source, [{"second": 450, "likes": 29, "text": "the nicest kid"}])
        again = FakeRunner()
        candidates = scan_source(source["id"], root=root, runner=again, find_peaks=peaks)
        kinds = [command[0] for command in again.commands]
        assert kinds.count("ffmpeg") == 1 and len(again.commands) == 2
        assert sorted(candidate["id"] for candidate in candidates) == ["c390", "c40"]


def test_scan_source_saves_every_candidate_with_a_rank():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source = make_source(root)
        candidates = scan_source(source["id"], root=root, runner=FakeRunner(), find_peaks=lambda audio, count: [(100, -8.0), (400, -7.5)])
        assert sorted(candidate["rank"] for candidate in candidates) == [1, 2]


def test_the_audio_download_asks_for_hls_first_in_parallel_fragments_and_retries():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source = make_source(root)
        runner = FakeRunner()
        scan_source(source["id"], root=root, runner=runner, find_peaks=lambda audio, count: [(100, -8.0)])
        command = runner.commands[0]
        # YouTube answers some plain https downloads with a 403 and throttles them to about 30 KiB/s; HLS was 7 times faster
        assert command[command.index("-f") + 1].startswith("ba[protocol^=m3u8]")
        assert command[command.index("-N") + 1] == "4" and "--retries" in command


def counting_command(folder: str, output: str, code: int) -> list:
    # a stand-in program: it appends one line to a counter file, prints some output and exits with the given code
    counter = Path(folder) / "attempts.txt"
    script = f"import sys; open({str(counter)!r}, 'a').write('x'); print({output!r}); sys.exit({code})"
    return [sys.executable, "-c", script]


def test_a_rate_limited_download_is_retried_then_reported_in_plain_words():
    saved = detect.RATE_LIMIT_WAITS
    detect.RATE_LIMIT_WAITS = (0, 0)
    try:
        with tempfile.TemporaryDirectory() as folder:
            try:
                run_command(counting_command(folder, "ERROR: Sign in to confirm you are not a bot HTTP Error 429", 1))
            except RateLimited as error:
                assert "YouTube" in str(error) and "wait" in str(error)
            else:
                raise AssertionError("no error")
            assert (Path(folder) / "attempts.txt").read_text() == "xxx"
    finally:
        detect.RATE_LIMIT_WAITS = saved


def test_a_rate_limit_that_clears_on_the_retry_succeeds():
    saved = detect.RATE_LIMIT_WAITS
    detect.RATE_LIMIT_WAITS = (0, 0)
    try:
        with tempfile.TemporaryDirectory() as folder:
            counter = Path(folder) / "attempts.txt"
            script = (f"import sys; c = {str(counter)!r}; n = len(open(c).read()) if __import__('os').path.exists(c) else 0; "
                      "open(c, 'a').write('x'); print('HTTP Error 429' if n == 0 else 'ok'); sys.exit(1 if n == 0 else 0)")
            run_command([sys.executable, "-c", script])
            assert counter.read_text() == "xx"
    finally:
        detect.RATE_LIMIT_WAITS = saved


def test_any_other_failure_is_not_retried():
    import subprocess

    with tempfile.TemporaryDirectory() as folder:
        try:
            run_command(counting_command(folder, "ERROR: Video unavailable", 1))
        except subprocess.CalledProcessError:
            pass
        else:
            raise AssertionError("no error")
        assert (Path(folder) / "attempts.txt").read_text() == "x"


def test_the_whisper_hint_is_names_only_because_a_game_sentence_made_whisper_echo_it():
    from whisper_settings import WHISPER_PROMPT

    # measured 2026-11-23 on two Fortnite picks: with "Tomo plays GTA 6 as Lucia and Jason" Whisper small returned 1 and 4
    # words instead of about 75; a plain list of names gave 76 and 72 and the same text on a GTA clip
    assert "GTA" not in WHISPER_PROMPT and "plays" not in WHISPER_PROMPT
    assert "IShowSpeed" in WHISPER_PROMPT and "Kai Cenat" in WHISPER_PROMPT


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
            print("ok", name)
