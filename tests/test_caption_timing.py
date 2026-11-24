import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from caption_timing import (MIN_UNCOVERED_SECONDS, apply_fixes, apply_timing, cue_key, is_hallucination, keep_segment, merge_words,
                            uncovered_speech)


def test_voiced_time_after_the_last_word_is_uncovered():
    assert uncovered_speech([(10.0, 10.4, "a")], [(10.0, 14.0)]) == [(10.55, 14.0)]


def test_voiced_time_between_two_words_and_after_them_is_uncovered():
    words = [(0.0, 1.0, "a"), (6.0, 7.0, "b")]
    assert uncovered_speech(words, [(0.0, 10.0)]) == [(1.15, 5.85), (7.15, 10.0)]


def test_voiced_time_every_word_covers_gives_nothing():
    assert uncovered_speech([(0.0, 1.0, "a"), (1.1, 2.0, "b")], [(0.0, 2.0)]) == []


def test_a_gap_shorter_than_the_minimum_is_ignored():
    assert uncovered_speech([(0.0, 1.0, "a")], [(0.0, 1.15 + MIN_UNCOVERED_SECONDS - 0.1)]) == []


def test_speech_with_no_word_at_all_is_uncovered_from_its_start():
    assert uncovered_speech([], [(3.0, 5.0)]) == [(3.0, 5.0)]


def test_a_segment_is_kept_when_it_sounds_like_speech_and_dropped_when_it_does_not():
    # measured on six stretches the first pass missed: speech had no_speech_prob 0.16 to 0.35, a laugh 0.67
    assert keep_segment(0.3, -0.8) and keep_segment(0.67, -0.83)
    assert not keep_segment(0.9, -0.5) and not keep_segment(0.2, -1.6)


def test_the_phrases_whisper_invents_on_noise_are_recognised():
    # seen in the recovered words of real clips: "for watching!" and "Subscribe to where the yard driving in somebody's"
    assert is_hallucination("Thanks for watching!") and is_hallucination("for watching!")
    assert is_hallucination("Subscribe to where the yard driving in somebody's") and is_hallucination("Subtitles by the Amara.org community")
    assert not is_hallucination("Oh my gosh") and not is_hallucination("I'm watching it right now")


def test_recovered_words_are_merged_in_time_order():
    words = [(0.0, 0.5, "a"), (6.0, 6.5, "b")]
    extra = [(2.0, 2.4, "x"), (3.0, 3.3, "y")]
    assert [text for _, _, text in merge_words(words, extra)] == ["a", "x", "y", "b"]


def test_a_fix_replaces_a_wrong_word_keeping_its_punctuation_and_ignoring_case():
    words = [(0.0, 0.3, "Are"), (0.4, 0.8, "Bitch."), (0.9, 1.2, "you")]
    assert apply_fixes(words, {"bitch": "bench"}) == [(0.0, 0.3, "Are"), (0.4, 0.8, "bench."), (0.9, 1.2, "you")]


def test_a_fix_with_an_empty_right_word_removes_the_word():
    assert apply_fixes([(0.0, 0.3, "uh"), (0.4, 0.8, "yes")], {"uh": ""}) == [(0.4, 0.8, "yes")]


def test_words_without_a_fix_and_no_fixes_at_all_are_left_alone():
    words = [(0.0, 0.3, "don't")]
    assert apply_fixes(words, {"bitch": "bench"}) == words and apply_fixes(words, None) == words


def test_a_timing_change_moves_the_cue_whose_original_start_matches():
    cues = [(1.0, 2.0, "A"), (3.0, 4.0, "B")]
    assert apply_timing(cues, {"3.00": {"start": 3.2, "end": 4.5}}) == [(1.0, 2.0, "A"), (3.2, 4.5, "B")]


def test_a_timing_change_for_a_cue_that_is_gone_and_no_timing_at_all_change_nothing():
    cues = [(1.0, 2.0, "A")]
    assert apply_timing(cues, {"9.00": {"start": 9.0, "end": 9.5}}) == cues and apply_timing(cues, None) == cues


def test_the_cue_key_is_its_start_with_two_decimals():
    assert cue_key(1.7999999999) == "1.80" and cue_key(0.0) == "0.00"


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
            print("ok", name)
