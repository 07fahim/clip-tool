"""When each caption is on screen: it follows the speech, so it ends at a sentence end, before a pause and where the voice stops.

Words come from words.py as [start, end, text]; speech spans (where a voice is heard) come from speech_spans.py.
"""
import re

from make_clips import WORDS_PER_CUE

# a caption is on screen only while someone speaks: it ends at the end of a sentence and before a pause. Measured on 15
# real clips: 334 of 1057 three-word captions ran across a pause over 0.5 s or a sentence end
PAUSE_BREAK_SECONDS = 0.5
# Whisper sometimes stretches a word's end into the sound after it (10.6 s seen); the median word is 0.22 s, the 95th 0.66 s.
# A word longer than this is checked against the speech detector's spans; without a span it is cut to this length
MAX_WORD_SECONDS = 1.2
# a long word inside real speech is kept, up to this length: the two words over 3 s in 3155 (8.3 s and 10.6 s) were timing
# errors, not held words, and the speech after the cut is transcribed again (uncovered_speech)
SPEECH_WORD_LIMIT_SECONDS = 3.0
# the detector ends a span a little early
SPEECH_PAD_SECONDS = 0.1
# the detector may start a span a little after the word does
SPEECH_START_SLACK_SECONDS = 0.15
SENTENCE_ENDINGS = ".?!"
# punctuation around a word that a fix keeps: the fix names the word, not its comma
WORD_EDGE = ".,?!:;\"“”…()"
# voiced time with no word is transcribed again when it lasts this long: 68 such stretches in 15 clips, 8% of the voiced
# time, and 5 of 6 tried held real speech that the first pass dropped
MIN_UNCOVERED_SECONDS = 0.5
# a second pass keeps a segment unless Whisper thinks it is not speech; measured on six stretches: speech 0.16 to 0.35,
# a laugh ("Hehehehe") 0.67. shortcut: 0.8 and -1.2 are guesses from six samples, tighten them if junk words appear
MAX_NO_SPEECH_PROB = 0.8
MIN_AVG_LOGPROB = -1.2
# phrases Whisper writes on noise from the videos it was trained on; two of them got through the probability check on real
# clips ("for watching!", "Subscribe to where the yard..."). shortcut: this list is what was seen, add what turns up
HALLUCINATION_PATTERN = re.compile(r"(?i)\b(for watching|subscribe|amara\.org|subtitles by)\b")


def cap_word_end(start: float, end: float, speech: list) -> float:
    if end - start <= MAX_WORD_SECONDS:
        return end
    span = next((span for span in speech if span[0] - SPEECH_START_SLACK_SECONDS <= start <= span[1] + SPEECH_START_SLACK_SECONDS), None)
    if span is None:
        return start + MAX_WORD_SECONDS
    return min(end, span[1] + SPEECH_PAD_SECONDS, start + SPEECH_WORD_LIMIT_SECONDS)


def group_words(words: list, speech: list = ()) -> list:
    cues, group = [], []
    for start, end, text in words:
        end = cap_word_end(start, end, speech)
        if group and (len(group) == WORDS_PER_CUE or start - group[-1][1] > PAUSE_BREAK_SECONDS
                      or group[-1][2].rstrip("\"'”’)")[-1:] in SENTENCE_ENDINGS):
            cues.append(group)
            group = []
        group.append((start, end, text))
    if group:
        cues.append(group)
    return [(group[0][0], group[-1][1], " ".join(word[2] for word in group)) for group in cues]


def uncovered_speech(words: list, speech: list) -> list:
    # the stretches of voiced time that no word covers, once each word's end is capped like the captions cap it
    ordered = sorted((start, cap_word_end(start, end, speech)) for start, end, _ in words)
    pieces = []
    for span_start, span_end in speech:
        cursor = span_start
        for start, end in ordered:
            if end + SPEECH_START_SLACK_SECONDS <= cursor or start >= span_end:
                continue
            if start - SPEECH_START_SLACK_SECONDS - cursor >= MIN_UNCOVERED_SECONDS:
                pieces.append((round(cursor, 2), round(start - SPEECH_START_SLACK_SECONDS, 2)))
            cursor = max(cursor, end + SPEECH_START_SLACK_SECONDS)
        if span_end - cursor >= MIN_UNCOVERED_SECONDS:
            pieces.append((round(cursor, 2), round(span_end, 2)))
    return pieces


def is_hallucination(text: str) -> bool:
    return bool(HALLUCINATION_PATTERN.search(text))


def keep_segment(no_speech_prob: float, avg_logprob: float) -> bool:
    return no_speech_prob <= MAX_NO_SPEECH_PROB and avg_logprob >= MIN_AVG_LOGPROB


def apply_fixes(words: list, fixes: dict) -> list:
    # the owner's corrections for words Whisper heard wrong; an empty right word removes the word
    if not fixes:
        return words
    lowered = {wrong.lower(): right for wrong, right in fixes.items()}
    fixed = []
    for start, end, text in words:
        right = lowered.get(text.strip(WORD_EDGE).lower())
        if right is None:
            fixed.append((start, end, text))
        elif right:
            front, back = text[:len(text) - len(text.lstrip(WORD_EDGE))], text[len(text.rstrip(WORD_EDGE)):]
            fixed.append((start, end, front + right + back))
    return fixed


def cue_key(start: float) -> str:
    # a cue is named by where it starts in the clip; its timing change is stored under that name
    return f"{start:.2f}"


def apply_timing(cues: list, timing: dict) -> list:
    # the owner's changes of when a caption starts and ends; a change for a cue that is gone is ignored
    if not timing:
        return cues
    changed = []
    for start, end, text in cues:
        change = timing.get(cue_key(start))
        changed.append((change["start"], change["end"], text) if change else (start, end, text))
    return changed


def merge_words(words: list, extra: list) -> list:
    return sorted([tuple(word) for word in words] + [tuple(word) for word in extra])
