import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))

import cut
from cut import approve_candidate, check_approval, queue_clip

# the fake downloads are not real videos, so the length check is answered here (one test below uses a short answer)
cut.video_seconds = lambda path: 1e9

SOURCE = {"id": "v_abc", "url": "https://www.youtube.com/watch?v=UeHMwf9vIss", "creator": "speed"}
CANDIDATE = {"id": "c340", "peak": 400, "loudness": -7.5, "start": 340, "end": 430, "text": "wow"}
APPROVAL = {"name": "speed_6_wow", "title": "SPEED SAID WOW", "hook": "Speed said wow", "body": "", "from": 30, "to": 75}


class Workspace:
    # a throwaway working folder: the code uses paths relative to the project root, so run inside a copy
    def __enter__(self):
        self.old = Path.cwd()
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        (root / "clips" / "speed").mkdir(parents=True)
        (root / "footage" / "picks").mkdir(parents=True)
        shutil.copyfile(PROJECT / "clips" / "presets.json", root / "clips" / "presets.json")
        (root / "clips" / "clips.json").write_text("[]", encoding="utf8")
        os.chdir(root)
        return root

    def __exit__(self, *error):
        os.chdir(self.old)
        self.folder.cleanup()


class FakeRunner:
    def __init__(self, fail=False):
        self.commands, self.fail = [], fail

    def __call__(self, command: list, env=None):
        self.commands.append(command)
        Path(command[command.index("-o") + 1]).write_bytes(b"video")
        if self.fail:
            raise RuntimeError("network")


def fake_render(clip: dict):
    path = Path("clips") / clip["streamer"] / f"{clip['name']}.mp4"
    path.write_bytes(b"clip")
    return path


def test_check_approval_refuses_bad_values():
    assert check_approval(APPROVAL, CANDIDATE) == []
    for change in ({"name": "has space"}, {"name": ""}, {"title": ""}, {"title": "X" * 29}, {"from": 75, "to": 30},
                   {"from": 30, "to": 33}, {"from": -1}, {"to": 91}, {"from": "a"}):
        assert check_approval(dict(APPROVAL, **change), CANDIDATE), change


def test_approve_downloads_the_section_adds_the_clip_and_renders_it():
    with Workspace() as root:
        runner = FakeRunner()
        clip = approve_candidate(SOURCE, CANDIDATE, APPROVAL, runner=runner, render=fake_render)
        command = runner.commands[0]
        assert "*368-417" in command and command[-1] == SOURCE["url"] and "--force-keyframes-at-cuts" in command
        # YouTube answers the plain https formats with a 403 to the ffmpeg that cuts sections; its HLS formats cut
        # cleanly (probed 2026-11-23), so HLS is asked for first
        assert command[command.index("-f") + 1].startswith("bv*[protocol^=m3u8]")
        assert (root / "footage" / "picks" / "v_abc_c340_28_77.mp4").is_file()
        saved = json.loads((root / "clips" / "clips.json").read_text(encoding="utf8"))
        assert saved[0]["segments"] == [[2, 47]] and saved[0]["pick"] == "v_abc_c340_28_77" and saved[0]["streamer"] == "speed"
        assert saved[0]["caption"] == "speed" and saved[0]["layout"] == "speed"
        assert (root / "clips" / "speed" / "speed_6_wow.mp4").is_file() and clip["name"] == "speed_6_wow"


def test_a_download_with_a_short_video_is_refused_and_leaves_no_file():
    # seen 2026-11-24: yt-dlp reported success on sections whose video held 5 to 30 s of a 62 s window
    import cut
    real = cut.video_seconds
    cut.video_seconds = lambda path: 5.0
    try:
        with Workspace() as root:
            try:
                approve_candidate(SOURCE, CANDIDATE, APPROVAL, runner=FakeRunner(), render=fake_render)
                assert False, "a short download was accepted"
            except RuntimeError as error:
                assert "short" in str(error)
            assert json.loads((root / "clips" / "clips.json").read_text(encoding="utf8")) == []
            assert list((root / "footage" / "picks").iterdir()) == []
    finally:
        cut.video_seconds = real


def test_a_failed_download_adds_no_clip_and_leaves_no_file():
    with Workspace() as root:
        try:
            approve_candidate(SOURCE, CANDIDATE, APPROVAL, runner=FakeRunner(fail=True), render=fake_render)
        except RuntimeError:
            pass
        assert json.loads((root / "clips" / "clips.json").read_text(encoding="utf8")) == []
        assert list((root / "footage" / "picks").iterdir()) == []


def test_a_clip_the_store_refuses_removes_its_download():
    with Workspace() as root:
        approve_candidate(SOURCE, CANDIDATE, APPROVAL, runner=FakeRunner(), render=fake_render)
        other = dict(CANDIDATE, id="c500", start=500, end=590)
        try:
            approve_candidate(SOURCE, other, APPROVAL, runner=FakeRunner(), render=fake_render)
        except ValueError as error:
            assert "already exists" in str(error)
        else:
            raise AssertionError("no error")
        assert [path.name for path in (root / "footage" / "picks").iterdir()] == ["v_abc_c340_28_77.mp4"]


def test_an_unknown_creator_is_refused_before_downloading():
    with Workspace():
        runner = FakeRunner()
        try:
            approve_candidate(dict(SOURCE, creator="nobody"), CANDIDATE, APPROVAL, runner=runner, render=fake_render)
        except ValueError as error:
            assert "creator" in str(error)
        assert runner.commands == []


def test_queue_clip_adds_one_entry_to_each_queue_and_refuses_a_second_time():
    with Workspace() as root:
        approve_candidate(SOURCE, CANDIDATE, APPROVAL, runner=FakeRunner(), render=fake_render)
        slots = queue_clip("speed_6_wow")
        youtube = json.loads((root / "clips" / "youtube_queue.json").read_text(encoding="utf8"))
        facebook = json.loads((root / "clips" / "facebook_queue.json").read_text(encoding="utf8"))
        assert len(youtube) == 1 and len(facebook) == 1
        assert youtube[0]["file"] == "clips/speed/speed_6_wow.mp4" and youtube[0]["title"] == "Speed said wow"
        assert facebook[0]["publish_at"] == slots["facebook"] and youtube[0]["publish_at"] == slots["youtube"]
        try:
            queue_clip("speed_6_wow")
        except ValueError as error:
            assert "already" in str(error)
        else:
            raise AssertionError("queued twice")


def test_queue_clip_needs_a_rendered_file():
    with Workspace() as root:
        approve_candidate(SOURCE, CANDIDATE, APPROVAL, runner=FakeRunner(), render=lambda clip: None)
        try:
            queue_clip("speed_6_wow")
        except ValueError as error:
            assert "render" in str(error)
        else:
            raise AssertionError("queued an unrendered clip")


def test_a_second_approval_of_the_same_moment_reuses_the_download_and_a_refusal_keeps_it():
    with Workspace() as root:
        runner = FakeRunner()
        approve_candidate(SOURCE, CANDIDATE, APPROVAL, runner=runner, render=fake_render)
        try:
            approve_candidate(SOURCE, CANDIDATE, APPROVAL, runner=runner, render=fake_render)
        except ValueError as error:
            assert "already exists" in str(error)
        assert len(runner.commands) == 1
        assert (root / "footage" / "picks" / "v_abc_c340_28_77.mp4").is_file()
        approve_candidate(SOURCE, CANDIDATE, dict(APPROVAL, name="speed_7_again"), runner=runner, render=fake_render)
        assert len(runner.commands) == 1


def test_queue_clip_adds_only_the_platform_that_is_missing():
    with Workspace() as root:
        approve_candidate(SOURCE, CANDIDATE, APPROVAL, runner=FakeRunner(), render=fake_render)
        (root / "clips" / "youtube_queue.json").write_text(json.dumps([{
            "file": "clips/speed/speed_6_wow.mp4", "title": "t", "description": "d",
            "publish_at": "2099-01-01T10:00:00+06:00"}]), encoding="utf8")
        slots = queue_clip("speed_6_wow")
        assert list(slots) == ["facebook"]
        assert len(json.loads((root / "clips" / "youtube_queue.json").read_text(encoding="utf8"))) == 1
        assert len(json.loads((root / "clips" / "facebook_queue.json").read_text(encoding="utf8"))) == 1


def test_two_queue_requests_at_once_queue_the_clip_once():
    import threading

    with Workspace() as root:
        approve_candidate(SOURCE, CANDIDATE, APPROVAL, runner=FakeRunner(), render=fake_render)
        outcomes = []

        def attempt():
            try:
                queue_clip("speed_6_wow")
                outcomes.append("queued")
            except ValueError:
                outcomes.append("refused")

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert outcomes.count("queued") == 1
        for name in ("youtube_queue.json", "facebook_queue.json"):
            assert len(json.loads((root / "clips" / name).read_text(encoding="utf8"))) == 1


def test_approve_stores_the_camera_crop_found_in_the_middle_of_the_segment():
    seen = []

    def finder(pick, second, default):
        seen.append((pick.name, second, default))
        return "crop=300:220:45:432"

    with Workspace() as root:
        clip = approve_candidate(SOURCE, CANDIDATE, APPROVAL, runner=FakeRunner(), render=fake_render, find_crop=finder)
        assert clip["cam_crop"] == "crop=300:220:45:432"
        assert seen[0][0] == "v_abc_c340_28_77.mp4" and seen[0][1] == 24.5 and seen[0][2].startswith("crop=")
        saved = json.loads((root / "clips" / "clips.json").read_text(encoding="utf8"))
        assert saved[0]["cam_crop"] == "crop=300:220:45:432"


def test_approve_without_a_found_crop_leaves_the_layouts_own_crop():
    with Workspace() as root:
        clip = approve_candidate(SOURCE, CANDIDATE, APPROVAL, runner=FakeRunner(), render=fake_render, find_crop=lambda *a: None)
        assert "cam_crop" not in clip
        # a crop nobody found is marked, so the Library can ask the owner to look at the frame
        assert clip["cam_unverified"] is True


def test_a_found_camera_crop_is_not_marked_unverified():
    with Workspace() as root:
        clip = approve_candidate(SOURCE, CANDIDATE, APPROVAL, runner=FakeRunner(), render=fake_render, find_crop=lambda *a: "crop=300:220:45:432")
        assert "cam_unverified" not in clip


def test_only_the_chosen_range_is_downloaded_not_the_whole_window():
    with Workspace():
        runner = FakeRunner()
        approve_candidate(SOURCE, CANDIDATE, dict(APPROVAL, **{"from": 10, "to": 40}), runner=runner, render=fake_render)
        # the window starts at 340: seconds 10 to 40 with 2 s of padding each side are stream seconds 348 to 382
        assert "*348-382" in runner.commands[0]


def test_the_padding_never_goes_outside_the_window():
    with Workspace():
        runner = FakeRunner()
        # near the start of the window the padding stops at 0, near the end at the window length (90 s, stream 430)
        approve_candidate(SOURCE, CANDIDATE, dict(APPROVAL, **{"from": 1, "to": 58}), runner=runner, render=fake_render)
        assert "*340-400" in runner.commands[0]
        approve_candidate(SOURCE, CANDIDATE, dict(APPROVAL, name="speed_8_late", **{"from": 31, "to": 89}), runner=runner, render=fake_render)
        assert "*369-430" in runner.commands[1]


def test_another_range_of_the_same_moment_is_downloaded_again():
    with Workspace():
        runner = FakeRunner()
        approve_candidate(SOURCE, CANDIDATE, APPROVAL, runner=runner, render=fake_render)
        approve_candidate(SOURCE, CANDIDATE, dict(APPROVAL, name="speed_7_other", **{"from": 10, "to": 40}), runner=runner, render=fake_render)
        assert len(runner.commands) == 2


def test_the_next_clip_downloads_while_the_last_one_renders_but_two_renders_never_overlap():
    import threading
    import time

    other = dict(CANDIDATE, id="c500", start=500, end=590)
    first_rendering, release, second_downloaded = threading.Event(), threading.Event(), threading.Event()
    state = {"active": 0, "peak": 0}

    class Runner(FakeRunner):
        def __call__(self, command: list, env=None):
            super().__call__(command, env)
            if "c500" in command[command.index("-o") + 1]:
                second_downloaded.set()

    def render(clip: dict):
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        if clip["name"] == "speed_6_wow":
            first_rendering.set()
            assert release.wait(5)
        else:
            time.sleep(0.2)
        state["active"] -= 1
        return fake_render(clip)

    with Workspace():
        runner = Runner()
        first = threading.Thread(target=lambda: approve_candidate(SOURCE, CANDIDATE, APPROVAL, runner=runner, render=render))
        second = threading.Thread(target=lambda: approve_candidate(SOURCE, other, dict(APPROVAL, name="speed_7_x"), runner=runner, render=render))
        first.start()
        assert first_rendering.wait(5)
        second.start()
        # the second clip's download finishes while the first is still rendering
        assert second_downloaded.wait(5)
        time.sleep(0.3)
        release.set()
        first.join(5)
        second.join(5)
        assert state["peak"] == 1


def test_a_clip_may_run_up_to_ninety_seconds_and_not_longer():
    assert check_approval(dict(APPROVAL, **{"from": 0, "to": 90}), CANDIDATE) == []
    assert check_approval(dict(APPROVAL, **{"from": 0, "to": 91}), dict(CANDIDATE, end=440))


def test_a_new_clip_gets_its_creators_bottom_line():
    with Workspace() as root:
        approve_candidate(SOURCE, CANDIDATE, APPROVAL, runner=FakeRunner(), render=fake_render)
        saved = json.loads((root / "clips" / "clips.json").read_text(encoding="utf8"))
        assert saved[0]["bottom_line"] == "follow for daily GTA 6 clips"


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
            print("ok", name)
