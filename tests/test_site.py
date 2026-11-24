from pathlib import Path

SITE = Path(__file__).resolve().parent.parent / "site"


def test_pages_exist_and_name_the_app():
    for name in ("index.html", "privacy.html"):
        assert "ViralPulse uploader" in (SITE / name).read_text(encoding="utf8")


def test_privacy_states_scope_and_storage():
    text = (SITE / "privacy.html").read_text(encoding="utf8")
    assert "youtube.upload" in text
    assert "stored on the owner’s own computer" in text
    assert "does not sell" in text


def test_privacy_has_no_placeholder():
    text = (SITE / "privacy.html").read_text(encoding="utf8")
    assert "TODO" not in text and "example.com" not in text


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
            print("ok", name)
