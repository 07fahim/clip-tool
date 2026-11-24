import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from sources import add_source, check_url, load_sources, parse_lines, save_source

GOOD_URL = "https://www.youtube.com/watch?v=UeHMwf9vIss"


def fake_fetch(url: str):
    return {"title": "A stream", "duration": 5400}


def test_check_url_accepts_youtube_links():
    assert check_url(GOOD_URL) == []
    assert check_url("https://youtu.be/UeHMwf9vIss") == []
    assert check_url("https://www.youtube.com/live/UeHMwf9vIss?si=abc") == []


def test_check_url_refuses_everything_else():
    for bad in ("file:///etc/passwd", "-o evil", "http://example.com/x", GOOD_URL + " --exec x",
                "https://www.youtube.com/watch?v=short", "https://evil.com/watch?v=UeHMwf9vIss", "", "--version"):
        assert check_url(bad), bad


def test_add_source_writes_a_record_and_a_second_add_returns_the_same_one():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        first = add_source(GOOD_URL, "speed", fetch=fake_fetch, root=root)
        assert first["title"] == "A stream" and first["duration"] == 5400
        assert first["creator"] == "speed" and first["status"] == "new"
        assert (root / first["id"] / "source.json").is_file()
        second = add_source(GOOD_URL, "speed", fetch=fake_fetch, root=root)
        assert second["id"] == first["id"]
        assert len(load_sources(root)) == 1


def test_add_source_refuses_a_bad_url_before_fetching():
    def never(url: str):
        raise AssertionError("fetched")

    with tempfile.TemporaryDirectory() as folder:
        try:
            add_source("file:///x", "speed", fetch=never, root=Path(folder))
        except ValueError as error:
            assert "youtube" in str(error).lower()
        else:
            raise AssertionError("no error")


def test_save_source_keeps_other_fields():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source = add_source(GOOD_URL, "speed", fetch=fake_fetch, root=root)
        source["status"] = "scanned"
        save_source(source, root)
        assert load_sources(root)[0]["status"] == "scanned"


def test_parse_lines_reads_windows_and_their_lines():
    text = "=== 0:01:00\n0:01:05 hello there\n0:01:09 wow\n=== 0:03:00\n0:03:02 hi\n"
    assert parse_lines(text) == [
        {"offset": 60, "lines": [(65, "hello there"), (69, "wow")]},
        {"offset": 180, "lines": [(182, "hi")]},
    ]


def test_parse_lines_skips_lines_it_cannot_read():
    assert parse_lines("junk first\n=== 0:00:10\nnot a clock\n0:00:12 ok\n") == [{"offset": 10, "lines": [(12, "ok")]}]


def test_parse_lines_skips_a_malformed_window_header():
    assert parse_lines("=== 0:00\n0:00:05 lost\n=== x:y:z\n=== 0:01:00\n0:01:02 ok\n") == [{"offset": 60, "lines": [(62, "ok")]}]


def test_check_url_refuses_a_second_video_id_in_the_query():
    assert check_url(GOOD_URL + "&v=AAAAAAAAAAA")
    assert check_url(GOOD_URL + "&t=30s") == []


def test_adding_a_known_link_with_another_creator_updates_the_creator():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        add_source(GOOD_URL, "speed", fetch=fake_fetch, root=root)
        again = add_source(GOOD_URL, "kai", fetch=fake_fetch, root=root)
        assert again["creator"] == "kai" and load_sources(root)[0]["creator"] == "kai"


def test_a_stream_that_is_still_live_or_not_started_is_refused_with_a_reason():
    # post_live: the stream has ended but YouTube is still processing the recording
    for state in ("is_live", "is_upcoming", "post_live"):
        with tempfile.TemporaryDirectory() as folder:
            try:
                add_source(GOOD_URL, "speed", fetch=lambda url: {"title": "t", "duration": 0, "live_status": state}, root=Path(folder))
            except ValueError as error:
                assert "live" in str(error).lower() and "ends" in str(error)
            else:
                raise AssertionError(state)
            assert load_sources(Path(folder)) == []


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
            print("ok", name)
