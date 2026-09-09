"""What the voice is actually handed.

Zeta's replies are Markdown because the chat panel renders them. The voice does not render
anything - it reads. Two real complaints drove this file:

* "the voice model is also reading things like astricks" - `**Done.**` came out as
  "star star Done star star", and a fenced code block was dictated character by character.
* long replies were a long silence, because Chatterbox returns nothing until the whole clip
  is finished. `segments()` is what lets playback start after the first sentence.
"""

from __future__ import annotations

import pytest

from app.emotion import speech


# ------------------------------------------------------------------ Markdown
@pytest.mark.parametrize("written, spoken", [
    ("**Done.** I've opened Chrome.", "Done. I've opened Chrome."),
    ("*maybe* not", "maybe not"),
    ("___really___ sure", "really sure"),
    ("run `npm install` first", "run npm install first"),
    ("~~never mind~~ it works", "never mind it works"),
    ("## Results\nAll good.", "Results.\nAll good."),
    ("> quoted line", "quoted line"),
    ("- one\n- two", "one.\ntwo."),
    ("1. first\n2. second", "first.\nsecond."),
    ("see [the pricing page](https://aws.amazon.com/pricing)", "see the pricing page"),
    ("![a chart](chart.png) is attached", "a chart is attached"),
    ("go to https://example.com/x now", "go to the link now"),
    ("---\nafter the rule", "after the rule"),
    ("nice 🎉 work", "nice work"),
])
def test_markdown_is_never_read_out(written, spoken):
    assert speech.strip_markdown(written) == spoken


def test_a_code_block_is_described_not_dictated():
    out = speech.strip_markdown("Here it is:\n```python\nfor i in range(10):\n    print(i)\n```\nThat's all.")
    assert "print" not in out and "```" not in out
    assert speech._CODE_SPOKEN in out and out.endswith("That's all.")


def test_underscores_inside_words_survive():
    """`some_variable_name` is one word, not italics. Mangling it would change what is said."""
    assert speech.strip_markdown("check some_variable_name in the file") == "check some_variable_name in the file"


def test_a_table_is_read_as_rows():
    table = "| model | vram |\n| --- | --- |\n| qwen3:8b | 5900 |"
    assert speech.strip_markdown(table) == "model, vram.\nqwen3:8b, 5900."


def test_markdown_removal_reaches_every_voice():
    """Chatterbox, ElevenLabs and the Windows voices all go through the same funnel."""
    style = speech.STYLES["neutral"]
    for model in ("chatterbox", "eleven_v3", "eleven_multilingual_v2"):
        assert "*" not in speech.prepare("**Done.** Opened it.", style, model)
    assert speech.for_voice("**Done.** Opened it.") == "Done. Opened it."


def test_delivery_tags_are_not_mistaken_for_markdown_links():
    assert speech.prepare("[sighs] fine", speech.STYLES["neutral"], "chatterbox") == "[sigh] fine"


# ------------------------------------------------------------------ splitting
def test_a_short_reply_is_one_piece():
    assert speech.segments("Done. I've opened Chrome.") == ["Done. I've opened Chrome."]


def test_the_first_piece_is_the_short_one():
    """It is the piece the person is waiting on, so it should be generated as fast as possible."""
    text = " ".join(f"This is sentence number {i} and it runs on for a while." for i in range(12))
    pieces = speech.segments(text)
    assert len(pieces) > 2
    assert len(pieces[0]) <= 160
    assert max(len(p) for p in pieces[1:]) > 160
    assert " ".join(pieces) == text          # nothing lost, nothing invented


def test_an_enormous_sentence_is_still_split():
    pieces = speech.segments("word " * 400)
    assert pieces and all(len(p) <= 300 for p in pieces)


def test_hindi_sentences_split_on_the_danda():
    pieces = speech.segments("मैं ठीक हूँ। आप कैसे हैं। सब बढ़िया है।", first_limit=13, limit=13)
    assert len(pieces) == 3


def test_nothing_to_say_is_no_pieces():
    assert speech.segments("   ") == []


# ------------------------------------------------------------------ language
def test_devanagari_is_recognised_as_hindi():
    assert speech.language_of("नमस्ते, आप कैसे हैं?") == "hi"


def test_english_asks_for_no_particular_language():
    assert speech.language_of("Hello, how are you?") == ""


def test_romanised_hinglish_is_not_claimed_to_be_hindi():
    """"kya haal hai" in Latin script is indistinguishable from English here, and pretending
    otherwise would send the wrong language id to the voice."""
    assert speech.language_of("kya haal hai") == ""


# ------------------------------------------------------------------ the endpoint
@pytest.mark.asyncio
async def test_the_plan_endpoint_hands_back_speakable_pieces():
    from app.api.routes.voice import speak_plan
    from app.models.schemas import SpeakRequest

    out = await speak_plan(SpeakRequest(text="**Done.** I've opened Chrome. Anything else?"))
    assert out["segments"] == ["Done. I've opened Chrome. Anything else?"]   # short enough for one clip
    assert out["truncated"] is False

    long = await speak_plan(SpeakRequest(text="**Right.** " + "This is a sentence about the thing. " * 20))
    assert len(long["segments"]) > 1
    assert not any("*" in piece for piece in long["segments"])


@pytest.mark.asyncio
async def test_an_essay_is_read_to_a_point_and_says_so():
    """At ~40 ms a character, a 20,000-character answer is thirteen minutes of talking."""
    from app.api.routes.voice import MAX_SPEECH_CHARS, speak_plan
    from app.models.schemas import SpeakRequest

    out = await speak_plan(SpeakRequest(text="This is a sentence. " * 900))
    assert out["truncated"] is True
    assert sum(len(s) for s in out["segments"]) <= MAX_SPEECH_CHARS


# ------------------------------------------------------------------ per-model sounds
def test_multilingual_never_gets_a_sound_it_would_read_out(monkeypatch):
    """Turbo performs [laugh]; multilingual says "Laugh." out loud. Same tag, same code path,
    so the allowed set has to depend on which model is loaded."""
    monkeypatch.setattr(speech, "_rng", type("D", (), {"random": lambda self: 0.0})())
    playful = speech.STYLES["playful"]                      # its sound is a chuckle
    assert speech.prepare("oh dear", playful, "chatterbox") == "[chuckle] oh dear"
    assert speech.prepare("oh dear", playful, "chatterbox-multilingual") == "oh dear"


def test_a_written_tag_the_model_cannot_perform_is_dropped_not_spoken():
    assert speech.prepare("[laughs] oh dear", speech.STYLES["neutral"], "chatterbox-multilingual") == "oh dear"
    assert speech.prepare("[sighs] oh dear", speech.STYLES["neutral"], "chatterbox-multilingual") == "[sigh] oh dear"


def test_the_model_is_only_told_about_cues_it_can_actually_make():
    note = speech.prompt_note("chatterbox-multilingual")
    assert "[sighs]" in note and "[laughs]" not in note and "[chuckles]" not in note
    assert "[laughs]" in speech.prompt_note("chatterbox")
