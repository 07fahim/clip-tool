import json
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))

from camera import MODEL_PATH, choose_face, crop_around, find_camera_crop, find_clip_crop, parse_crop

DEFAULT = "crop=580:330:1340:470"


def test_parse_crop_reads_x_y_width_height():
    assert parse_crop(DEFAULT) == (1340, 470, 580, 330)


def test_choose_face_ignores_faces_far_from_the_default_box():
    box = (1340, 470, 580, 330)
    near, far = (1500, 520, 100, 120, 0.9), (100, 100, 300, 300, 0.95)
    assert choose_face([far, near], box) == near
    assert choose_face([far], box) is None


def test_choose_face_takes_the_largest_near_face_and_ignores_weak_detections():
    box = (1340, 470, 580, 330)
    small, large, weak = (1500, 520, 60, 70, 0.9), (1480, 500, 110, 130, 0.9), (1500, 520, 300, 300, 0.3)
    assert choose_face([small, weak, large], box) == large


def test_crop_around_centres_the_default_size_on_the_face_and_stays_inside_the_frame():
    assert crop_around((1500, 520, 100, 120, 0.9), (580, 330), (1920, 1080)) == "crop=580:330:1260:415"
    assert crop_around((1880, 520, 100, 120, 0.9), (580, 330), (1920, 1080)) == "crop=580:330:1340:415"
    assert crop_around((10, 10, 100, 120, 0.9), (580, 330), (1920, 1080)) == "crop=580:330:0:0"


def test_find_camera_crop_uses_the_detector_and_gives_nothing_without_a_near_face():
    detect = lambda path: ([(1500, 520, 100, 120, 0.9)], 1920, 1080)
    # a face that lies inside the usual box keeps the usual (hand-measured) box: re-centring on the face moved it up
    # until it showed the stream overlay above the camera
    assert find_camera_crop(Path("x.png"), DEFAULT, detect=detect) == DEFAULT
    outside = lambda path: ([(1850, 520, 100, 120, 0.9)], 1920, 1080)
    assert find_camera_crop(Path("x.png"), DEFAULT, detect=outside) == "crop=580:330:1340:415"
    assert find_camera_crop(Path("x.png"), DEFAULT, detect=lambda path: ([(50, 50, 100, 100, 0.9)], 1920, 1080)) is None
    assert find_camera_crop(Path("x.png"), DEFAULT, detect=lambda path: ([], 1920, 1080)) is None


def test_the_model_path_does_not_depend_on_the_folder_the_tool_was_started_from():
    assert MODEL_PATH.is_absolute() and MODEL_PATH.parent == PROJECT


def test_a_frame_that_is_not_1920_by_1080_gives_no_crop_instead_of_one_outside_the_frame():
    # the layouts' boxes are measured on a 1920x1080 stream; a 720p frame has the same face at other pixels
    detect = lambda path: ([(900, 250, 60, 70, 0.9)], 1280, 720)
    assert find_camera_crop(Path("x.png"), DEFAULT, detect=detect) is None


def test_find_clip_crop_never_raises_on_a_file_that_is_not_a_video():
    with tempfile.TemporaryDirectory() as folder:
        junk = Path(folder) / "junk.mp4"
        junk.write_bytes(b"not a video")
        assert find_clip_crop(junk, 5, DEFAULT) is None


def iou(a: tuple, b: tuple) -> float:
    ix, iy = max(a[0], b[0]), max(a[1], b[1])
    ox, oy = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    inter = max(ox - ix, 0) * max(oy - iy, 0)
    return inter / (a[2] * a[3] + b[2] * b[3] - inter)


def test_the_real_model_finds_tomo_s_camera_in_a_real_pick():
    # reads the real footage and model without changing them; skipped on a machine that does not have them
    pick, model = PROJECT / "footage" / "picks" / "tomo_lucia.mp4", PROJECT / "yunet.onnx"
    if not (pick.is_file() and pick.stat().st_size > 10000 and model.is_file()):
        print("skipped: no real pick or model")
        return
    layout = json.loads((PROJECT / "clips" / "presets.json").read_text(encoding="utf8"))["layouts"]["tomo"]["cam_crop"]
    import os

    old = os.getcwd()
    os.chdir(PROJECT)
    try:
        found = find_clip_crop(pick, 21, layout)
    finally:
        os.chdir(old)
    assert found is not None
    # measured on 15 real clips: the face-centred crop overlaps the hand-measured one by 0.82 on average (lowest 0.46)
    assert iou(parse_crop(found), parse_crop(layout)) > 0.6


def test_a_wider_search_margin_finds_a_face_far_from_the_usual_spot():
    box = (1340, 470, 580, 330)
    far = (100, 100, 300, 300, 0.95)
    assert choose_face([far], box) is None and choose_face([far], box, margin=5) == far
    detect = lambda path: ([far], 1920, 1080)
    assert find_camera_crop(Path("x.png"), DEFAULT, detect=detect) is None
    assert find_camera_crop(Path("x.png"), DEFAULT, detect=detect, margin=5) == "crop=580:330:0:85"


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
            print("ok", name)
