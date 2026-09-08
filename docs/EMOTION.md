# Emotional intelligence

Zeta reads how you seem before it decides how to answer, and it does that entirely on your
machine. There is no emotion API, no upload, no third-party model. Two cheap local analyses run
on every turn (roughly a millisecond for text, tens of milliseconds for audio), and the result is
compressed into one plain-English line that is added to the prompt for that turn only.

## What it looks at

| Channel | Source | What it gives |
|---------|--------|---------------|
| Words | `app/emotion/lexicon.py` | A compact lexicon plus multi-word phrases, negation ("not great"), intensifiers ("really"), downtoners ("a bit"), shouting, emoji, and the audio-event tags Scribe emits for real laughter or sighs |
| Tone of voice | `app/emotion/prosody.py` | Pitch (autocorrelation), loudness dynamics, syllable rate, pauses and tremor, compared against a rolling baseline of how *you* usually sound |
| Safety | `app/emotion/crisis.py` | Tiered detection of serious distress, with idiom guards so "this deadline is killing me" is never treated as a crisis |

The two channels are fused with confidence weights (`app/emotion/engine.py`): the voice leads on
energy, the words lead on whether things are good or bad. Agreement between them raises confidence;
contradiction lowers it and is reported as a cue ("words and tone disagree"). The result is a point
on the valence/arousal circumplex plus a label, an intensity, and the cues that produced it.

## What Zeta does with it

* **The reply.** A short note goes into the prompt: *"How they seem right now: probably frustrated,
  blocked (worn down by something, nothing is working)."* The system prompt tells the model to let
  it change *how* it answers, never to announce the analysis, never to diagnose, and to acknowledge
  before it problem-solves. If the note and the words disagree, the words win.
* **The delivery.** `app/emotion/speech.py` maps the reading onto whichever voice is in use.
  Chatterbox gets `exaggeration` (how much emotion to act) and `cfg_weight` (pacing: lower is slower),
  so someone who sounds low hears a calmer, slower Zeta. ElevenLabs gets `voice_settings` (stability
  and style). Windows voices get a pace change. The written reply is identical either way.
* **The Mood panel.** The right drawer shows the current reading, the direction of the conversation,
  a sparkline of the last two weeks, and the log of recent readings. The orb tints with your mood.
* **Memory of the trend.** Readings are stored in the `emotion_log` table, so "your mood has been
  sinking across this conversation" is a fact, not a guess.

## Laughs, giggles and sighs

**With Chatterbox (the default voice) they are free and local.** It performs `[laugh]`, `[chuckle]`,
`[giggle]`, `[sigh]`, `[gasp]` and `[whisper]`. Zeta translates its own tag names to that dialect,
drops the ones Chatterbox cannot do (mood tags such as `[warmly]` are expressed through pacing
instead), and never lets a tag reach the text you read.

### If you use ElevenLabs instead: do you need Pro?

You do not. Inline delivery tags are a property of the *model*, not the plan. Measured on a free-tier
key with a premade voice:

| Model | `[laughs] That is wonderful news.` comes back as | Latency |
|-------|--------------------------------------------------|---------|
| `eleven_v3` | "That is wonderful news" - the tag is **performed**, not spoken | ~2.2 s |
| `eleven_multilingual_v2` | "**Laughs.** That is wonderful news" - it reads the tag out loud | ~1.8 s |
| `eleven_flash_v2_5` | "That is wonderful news" - the tag is dropped, nothing performed | ~1.1 s |

(Verified by synthesizing the line and transcribing the audio back through Scribe.)

So Zeta only ever sends tags to a v3 model, and strips them for every other model and for the text
you read. Set `ELEVENLABS_MODEL=eleven_v3` (or pick **Expressive** in the setup wizard) to let Zeta
laugh; the cost is about a second of extra latency per reply. What a paid plan actually buys you is
the voice *library* (custom and cloned voices) and higher character limits, not the tags.

## Settings

```
EMOTION_ENABLED=true        # read emotion at all
EMOTION_PROSODY=true        # also analyse tone of voice on spoken turns
EMOTION_ADAPT_VOICE=true    # adapt the spoken delivery
EMOTION_REGION=in           # which helplines to offer: in | us | uk | intl
EMOTION_SUPPORT_MODE=true   # let support take priority over task efficiency
ELEVENLABS_MODEL=eleven_v3  # optional: the model that performs [laughs]
```

## API

| Endpoint | Purpose |
|----------|---------|
| `GET /api/emotion` | Current reading, trend, voice baseline, whether the voice is expressive |
| `GET /api/emotion/history?limit=&days=` | The reading log |
| `GET /api/emotion/daily?days=` | Average valence/arousal per day, for the sparkline |
| `POST /api/emotion/analyze` | Analyse a piece of text without involving the agent |

A `emotion` event is also pushed over the WebSocket when a reading is confident enough. It is
deliberately **not** written into the activity feed: how you feel is not tool output.

## Limits, honestly

* This is a heuristic, not a model of your mind. It is confident about shouting, laughter, explicit
  feeling words and obvious distress; it is weak on sarcasm, on dry humour and on a language other
  than English. Low-confidence readings produce no prompt note at all.
* Tone of voice needs three or four spoken turns before the baseline means anything. Until then only
  the strongest acoustic cues count, and confidence stays low.
* Zeta is not a therapist, a doctor or a crisis service, and the prompt says so. In the `crisis` and
  `emergency` tiers it stays with the person, avoids tools and cheerfulness, and points once to real
  human help (Tele-MANAS 14416 and AASRA in India, 988 in the US, Samaritans 116 123 in the UK).
* Everything stays local. The readings never leave your machine, and nothing but the one-line hint
  reaches the model - which, for a local Ollama model, is also your machine.
