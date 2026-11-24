"""Find where the streamer's camera is in a frame: a face near the layout's usual camera box, then a crop of the same
size centred on it. Needs OpenCV and the YuNet model (yunet.onnx in the project folder); without them it finds nothing.
"""
import logging
import re
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger("camera")

# beside the scripts folder, so it is found whatever folder the tool was started from
MODEL_PATH = Path(__file__).resolve().parent.parent / "yunet.onnx"
# the layouts' camera boxes are measured on a 1920x1080 stream
STREAM_FRAME = (1920, 1080)
CROP_PATTERN = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")
# a face counts as the streamer's when its centre is within this share of the box size beyond the box: cams move a
# little within a stream, while game characters elsewhere on the screen must not count
SEARCH_MARGIN = 0.5
# faces scored 0.78 to 0.94 on 15 real clips; weaker detections are more likely patterns in the game picture
MIN_FACE_SCORE = 0.6
FRAME_TIMEOUT_SECONDS = 60


def parse_crop(crop: str) -> tuple:
    width, height, x, y = (int(value) for value in CROP_PATTERN.fullmatch(crop).groups())
    return x, y, width, height


def choose_face(faces: list, box: tuple, margin: float = SEARCH_MARGIN):
    box_x, box_y, box_width, box_height = box
    near = [face for face in faces
            if face[4] >= MIN_FACE_SCORE
            and box_x - box_width * margin <= face[0] + face[2] / 2 <= box_x + box_width * (1 + margin)
            and box_y - box_height * margin <= face[1] + face[3] / 2 <= box_y + box_height * (1 + margin)]
    return max(near, key=lambda face: face[2] * face[3], default=None)


def crop_around(face: tuple, size: tuple, frame_size: tuple) -> str:
    width, height = size
    centre_x, centre_y = face[0] + face[2] / 2, face[1] + face[3] / 2
    x = max(min(int(centre_x - width / 2), frame_size[0] - width), 0)
    y = max(min(int(centre_y - height / 2), frame_size[1] - height), 0)
    return f"crop={width}:{height}:{x}:{y}"


def detect_faces(frame_path: Path) -> tuple:
    import cv2

    image = cv2.imread(str(frame_path))
    height, width = image.shape[:2]
    detector = cv2.FaceDetectorYN.create(str(MODEL_PATH), "", (width, height), MIN_FACE_SCORE, 0.3, 5000)
    _, found = detector.detect(image)
    faces = [] if found is None else [(row[0], row[1], row[2], row[3], row[-1]) for row in found]
    return faces, width, height


def find_camera_crop(frame_path: Path, default_crop: str, detect=detect_faces, margin: float = SEARCH_MARGIN):
    box = parse_crop(default_crop)
    faces, width, height = detect(frame_path)
    # a smaller stream (720p) puts the same camera at other pixels, so the box and the crop would both be wrong
    if (width, height) != STREAM_FRAME:
        logger.warning(f"frame is {width}x{height}, not {STREAM_FRAME[0]}x{STREAM_FRAME[1]}: camera not searched")
        return None
    face = choose_face(faces, box, margin)
    if face is None:
        return None
    # a face inside the usual box means the usual box is right: it is hand-measured to the camera feed, while a crop
    # centred on the face is not and showed the stream overlay above the feed on a real clip
    if face[0] >= box[0] and face[1] >= box[1] and face[0] + face[2] <= box[0] + box[2] and face[1] + face[3] <= box[1] + box[3]:
        return default_crop
    return crop_around(face, (box[2], box[3]), (width, height))


def find_clip_crop(pick_path: Path, second: float, default_crop: str, margin: float = SEARCH_MARGIN):
    # never raises: a missing OpenCV or model, a bad file or no face just means the layout's own crop is used
    try:
        with tempfile.TemporaryDirectory() as folder:
            frame = Path(folder) / "frame.png"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(second), "-i", str(pick_path), "-frames:v", "1", str(frame)],
                           check=True, timeout=FRAME_TIMEOUT_SECONDS)
            return find_camera_crop(frame, default_crop, margin=margin)
    except Exception as error:
        logger.warning(f"camera crop not found for '{pick_path.name}': {error!r}")
        return None
