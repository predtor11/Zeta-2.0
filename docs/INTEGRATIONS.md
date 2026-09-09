# Voice, messaging, email and developer integrations

## LLM providers

| `LLM_PROVIDER` | Needs | Notes |
|----------------|-------|-------|
| `ollama` | Ollama running, `ollama pull qwen3:8b` (or llama3.1, qwen2.5…) | Native `/api/chat`, tool calling. `LLM_CONTEXT_LENGTH` (default 16384) must exceed Ollama's 4096 default or the tool definitions get truncated and the model "can't access" anything. `LLM_THINK=true` enables qwen3/deepseek reasoning mode (slower). |
| `lmstudio` | LM Studio "Local server" started | OpenAI-compatible on `http://localhost:1234/v1` |
| `openai` | `OPENAI_API_KEY` | `LLM_MODEL=gpt-4o-mini` etc. |
| `anthropic` | `ANTHROPIC_API_KEY` | Messages API, tool use, vision |
| `openai_compatible` | `LLM_BASE_URL`, `LLM_API_KEY` | Groq, OpenRouter, Together, vLLM, llama.cpp server |
| `mock` | nothing | Tests and UI work without a model |

Optional: `EMBEDDING_PROVIDER=ollama` + `ollama pull nomic-embed-text` for semantic memory and file
search; `LLM_VISION_MODEL=qwen2.5vl:7b` (or `llava`) for `analyze_screen`.

## Speech-to-text (free, local)

`STT_PROVIDER=whisper` runs faster-whisper on your machine. **Put it on the GPU** - the difference is
not subtle:

| Model | Device | Time for a 2.6 s clip |
|-------|--------|-----------------------|
| `small` | CPU (int8) | 7.1 s |
| `small` | CUDA (float16) | 0.6 s |
| `tiny` | CPU (int8) | 0.3 s |

```
backend\.venv\Scripts\python -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

That is all: Zeta adds those pip folders to the Windows DLL search path itself before loading the
model, so `STT_DEVICE=auto` then picks CUDA (and still falls back to the CPU if the GPU is busy).
Whisper `small` needs about 0.5 GB of VRAM. The model is loaded during start-up, so the first thing
you say is not the slow one.


`STT_PROVIDER=whisper` uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper), installed
with `pip install faster-whisper` into `backend/.venv`. The model (`STT_MODEL`: `tiny`, `base`,
`small`, `medium`, `large-v3`) downloads automatically on first use (~500 MB for `small`).
Browser audio (webm/opus) is decoded by PyAV, which ships with faster-whisper; `ffmpeg` on PATH
is optional. GPU is used automatically if CUDA + cuDNN are available, otherwise CPU (int8).

Alternative: `STT_PROVIDER=openai` with any `/v1/audio/transcriptions` server.

## Text-to-speech

| `TTS_PROVIDER` | Setup | Cost |
|----------------|-------|------|
| `elevenlabs` | `ELEVENLABS_API_KEY` from elevenlabs.io → Profile → API keys. Optional `TTS_VOICE=<voice id>` (list with `GET /api/voice/voices`), `ELEVENLABS_MODEL` | Free tier ~10k credits/month, then paid |
| `local` | Windows built-in voices, nothing to install. `TTS_VOICE="Microsoft Zira Desktop"` | Free |
| `piper` | piper.exe + a `.onnx` voice; `PIPER_EXECUTABLE`, `TTS_VOICE=path.onnx` | Free |

Replies are spoken automatically in the UI when TTS is enabled (mute button under the mic).

## WhatsApp

| `WHATSAPP_PROVIDER` | How it works | Cost |
|---------------------|--------------|------|
| `web` | Zeta drives web.whatsapp.com in its own Chrome profile. The first send opens the window; scan the QR code with your phone (WhatsApp → Linked devices). The session persists in `data/browser_profile`. Send + read recent messages. Keep `BROWSER_HEADLESS=false`. | **Free**. Unofficial automation; use for personal, low-volume messaging. |
| `business` | Meta WhatsApp Cloud API: create a Meta developer app, add the WhatsApp product, get a permanent token and phone number id → `WHATSAPP_BUSINESS_TOKEN`, `WHATSAPP_BUSINESS_PHONE_ID`. Send only. | Free tier: 1000 service conversations/month; outside the 24h window only approved templates can be sent. Needs a business phone number. |
| `wapi` | Generic HTTP gateway (UltraMsg, green-api, wapi-style): `WAPI_BASE_URL`, `WAPI_TOKEN`, `WAPI_INSTANCE_ID`. Subclass `WAPIProvider` in a plugin to adapt the request shape. | Usually a small monthly fee after a trial. |

Contacts: copy `backend/config/contacts.example.json` to `backend/config/contacts.json`. The model
must resolve names through `resolve_contact`; ambiguous names go back to the user. Sends are
SENSITIVE and confirm by default with the resolved recipient and full text.

## Email

Two ways to authenticate; both are free.

### OAuth (recommended, `EMAIL_AUTH=oauth`)

Zeta implements the OAuth 2.0 authorization-code flow with PKCE and signs in to IMAP/SMTP with
XOAUTH2. Tokens are stored in `data/oauth_tokens.json` and refreshed automatically. Connect from
**Settings → step 4 (Integrations) → Connect**, or `POST /api/email/oauth/start?provider=gmail`.

**Gmail** (`EMAIL_PROVIDER=gmail`):
1. Go to https://console.cloud.google.com → create a project.
2. APIs & Services → OAuth consent screen → External → fill name/email → add your Gmail address under
   *Test users* (required while the app is in "Testing"; no verification needed for personal use).
3. APIs & Services → Credentials → Create credentials → OAuth client ID → Application type **Desktop app**.
4. Put the client ID and secret in `.env`: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`.
5. Restart Zeta, open Settings → Integrations → Connect Gmail → approve in the browser.
   Google redirects to `http://localhost:8765/api/email/oauth/callback` (loopback is allowed for Desktop clients).

**Outlook / Microsoft 365** (`EMAIL_PROVIDER=outlook`):
1. https://entra.microsoft.com → App registrations → New registration; supported account types
   "Personal Microsoft accounts and any organizational directory".
2. Authentication → Add platform → **Mobile and desktop applications** → custom redirect URI
   `http://localhost:8765/api/email/oauth/callback`; set *Allow public client flows* = Yes.
3. API permissions → Add → APIs my organization uses → "Office 365 Exchange Online" →
   Delegated → `IMAP.AccessAsUser.All`, `SMTP.Send`; plus Microsoft Graph `offline_access`, `openid`, `email`.
4. `.env`: `MICROSOFT_CLIENT_ID=<Application (client) ID>`, `MICROSOFT_TENANT=common`.
5. Restart, Settings → Integrations → Connect Outlook.

### App password (`EMAIL_AUTH=password`)

Gmail: enable 2-step verification → https://myaccount.google.com/apppasswords → create a 16-character
password → `EMAIL_PASSWORD`. Outlook.com: Security → Advanced security → App passwords. Generic IMAP:
`EMAIL_PROVIDER=imap` with `EMAIL_IMAP_HOST`, `EMAIL_SMTP_HOST`.

Tools: `list_emails`, `read_email`, `draft_email`, `send_email` (confirms), `download_email_attachment`.
Email bodies are untrusted content.

## Developer tools

`git_status`, `git_diff`, `git_log`, `docker_status`, `docker_logs`, `inspect_project` and
`github_query` (`GITHUB_TOKEN` for private repos: GitHub → Settings → Developer settings →
Personal access tokens). Mutations (`git push`, `docker restart`, `npm install`) go through
`execute_command` and are classified SENSITIVE (confirm) or DANGEROUS.

## Browser

Playwright Chromium (uses installed Chrome when present) with a persistent profile in
`data/browser_profile`. Run `backend\.venv\Scripts\python -m playwright install chromium` once if
the bundled browser is missing. `web_search` uses DuckDuckGo over HTTP first and falls back to the
browser; `http_get` fetches APIs without a browser.

## Wake word ("Hey Zeta", hands-free)

Fully local. A background thread listens to the microphone; when it hears the phrase it beeps,
publishes a `wake_word` event, and the UI records your request automatically (stops after ~1.2 s of
silence) and sends it through the normal voice pipeline. Audio never leaves the machine.

```
backend\.venv\Scripts\pip install sounddevice numpy      # faster-whisper is needed too (STT_PROVIDER=whisper)
WAKE_WORD_ENABLED=true
WAKE_WORD=hey zeta
```

Engines (`WAKE_WORD_ENGINE`):

| Engine | How | Cost | Notes |
|---|---|---|---|
| `whisper` (default) | Energy-gated: when speech is heard, the last ~2.5 s are transcribed with faster-whisper **tiny** (CPU, int8) and fuzzy-matched | free, some CPU while people talk | Works for any phrase, no training. "Zeta", "hey zita", "hey zetta" all match. |
| `openwakeword` | Neural wake-word model | free, very low CPU | `pip install openwakeword`; pretrained phrases are `hey_jarvis`, `alexa`, `hey_mycroft`… (`WAKE_WORD_MODEL=hey_jarvis`). A custom "hey zeta" model needs training (openWakeWord's notebook). |

### Not waking up on its own

A tiny Whisper model handed silence does not stay silent - it writes something. Four rules decide
whether a candidate counts, and `WAKE_WORD_SENSITIVITY` (0-1, default 0.5) moves all four together:

1. **Whisper's VAD runs first.** Room noise produces no segments at all, so there is nothing to
   mis-read. Decoding is greedy at temperature 0.
2. **Whisper's own confidence is believed.** A segment is dropped when `no_speech_prob` is high or
   `avg_logprob` is low - the model saying, in effect, "I made that up".
3. **Stock hallucinations are rejected**: "you", "Thanks for watching", "so", and any transcript that
   repeats itself ("hey zeta hey zeta hey zeta"), which is a decoder looping on its own bias.
4. **The name alone only counts in a short utterance.** "Zeta?" wakes it; "the zeta function is what
   he meant" does not. Words that sound like the name but are ordinary English - *theta, beta, data,
   meta* - need the greeting in front of them, and a greeting slot filled by *the*, *a*, *of*… is not
   a greeting.

There is no `initial_prompt` stuffed with the wake phrase. That is a natural way to make "Zeta"
recognisable, and it was in Zeta until it turned out to be the reason it woke itself up: given
nothing to transcribe, the model repeats its own prompt back. `hotwords` does the same job without
inviting that.

Zeta also deafens the listener for exactly as long as a spoken reply plays (measured from the WAV,
not guessed from the text length), so it never hears itself through the speakers.

Check it: `GET /api/voice/wake` shows the state, engine, errors and microphone list (`WAKE_WORD_DEVICE`
picks one), plus `rejected` and `last_rejected` - if Zeta is not waking when you speak, that is where
to look before raising the sensitivity. `POST /api/voice/wake/test` simulates a detection.

Keep the browser tab open: detection happens in the backend, but the recording/transcription of your
request is done by the UI (microphone permission is remembered after the first manual mic use).

## Database: local SQLite or Supabase / PostgreSQL

Conversations, messages, tasks, long-term memory, the audit log and schedules live in one
SQLAlchemy database. Default is SQLite at `data/zeta.db` (WAL mode, zero setup, works offline).
The file search index (`data/file_index.db`, SQLite FTS5) is separate and always local: it mirrors
this computer's disk and is rebuilt on demand.

To use Supabase (free tier is enough; ~500 MB):

1. Supabase dashboard -> Project Settings -> Database -> **Connection string** -> URI. Prefer the
   *Transaction* pooler (port 6543). Copy it and replace `[YOUR-PASSWORD]`.
2. `backend\.venv\Scripts\pip install asyncpg`
3. Optional, copy existing data: `python scripts/migrate_db.py --to "<uri>"` (creates the tables,
   skips rows that already exist, safe to re-run).
4. `.env`: `DATABASE_URL=<uri>` (or Settings -> step 3 -> Advanced -> Database), restart Zeta.
   Settings -> status panel shows "Database: Supabase" when connected; `GET /api/system/database`
   gives details.

Zeta rewrites `postgres://`/`postgresql://` to the asyncpg driver, applies SSL for remote hosts,
strips `sslmode=`, and disables prepared statements on the Supabase pooler (PgBouncer). Any other
PostgreSQL works the same way. Trade-off: every message and audit row becomes a network round-trip,
so keep SQLite unless you need the data off-machine (cloud mode, several machines, dashboards).

## ElevenLabs voices

Free accounts may use ElevenLabs' **premade** voices through the API; "library" voices (including
the old default *Rachel*) return `paid_plan_required`. `TTS_VOICE` accepts a name (`george`,
`daniel`, `sarah`, `brian`, `alice`, `charlie`, `lily`, `matilda`, `liam`, `will`, `jessica`, `eric`,
`chris`, `laura`, `callum`) or any voice id. If a voice is rejected Zeta falls back to George. A
restricted key only needs the *Text to Speech* permission; `voices_read` is optional.

## Chatterbox: Zeta's own voice (local, free, clonable)

`TTS_PROVIDER=chatterbox` runs [Resemble AI's Chatterbox](https://github.com/resemble-ai/chatterbox)
on your own GPU. No API key, no quota, nothing leaves the machine, and any voice can be cloned from a
single short clip with no training.

```
install_tts.bat        # once: creates .venv-tts, installs PyTorch (CUDA) + chatterbox-tts
start_tts.bat          # optional: run the voice server yourself and watch its log
```

The model lives in a **separate process and virtualenv** (`scripts/chatterbox_server.py`, port 8766),
so the backend keeps its fast start-up and stays swappable. Zeta starts that process by itself when it
first needs it (`CHATTERBOX_AUTOSTART=true`), logs it to `logs/chatterbox.log`, and stops it on shutdown.
If you already have a server on that URL (a `start_tts.bat` window, say), Zeta uses it and leaves it alone.

| Setting | Meaning |
|---------|---------|
| `CHATTERBOX_MODEL` | `turbo` (350M, English, fastest), `base`, or `multilingual` (500M, 23 languages including Hindi) |
| `CHATTERBOX_DEVICE` | `auto` (CUDA if it fits, else CPU), `cuda`, `cpu` |
| `CHATTERBOX_VOICE` | Reference clip, e.g. `voice/zeta_female.wav`. Empty = the model's built-in voice |
| `CHATTERBOX_BASE_URL` | Where the voice server listens (default `http://127.0.0.1:8766`) |
| `CHATTERBOX_AUTOSTART` | Start the server on demand instead of requiring `start_tts.bat` |
| `CHATTERBOX_EXAGGERATION` / `CHATTERBOX_CFG_WEIGHT` | Baseline emotion and pacing; the emotion engine overrides them per reply |
| `CHATTERBOX_LANGUAGE` | `multilingual` only. Empty = detect from the reply (Devanagari means Hindi) |

**Switching to `multilingual` downloads a different model.** Turbo/base and multilingual share a
repo but not their weights: multilingual pulls `t3_mtl23ls_v2.safetensors`, `s3gen.pt`, `ve.pt`,
`grapheme_mtl_merged_expanded_v1.json` and `Cangjie5_TC.json`, about 3 GB on top of what you already
have. The server does it on first start, so the first launch after the switch is a long one.

If your network blocks huggingface.co (the API answers but transfers stall), fetch them first with
`HF_ENDPOINT=https://hf-mirror.com` and `HF_HUB_DISABLE_XET=1`. Ask for **each file by name** with
`hf_hub_download`: against the mirror, `snapshot_download` returns success in about two seconds
having fetched nothing, because its repo listing comes back empty and every pattern matches nothing.
It looks exactly like a fast cache hit.

**Changing the voice.** Put a clean 7-15 second WAV of the voice you want in `voice/`, set
`CHATTERBOX_VOICE=voice/your_clip.wav`, and restart the voice server. One speaker, no music, no echo.
Only clone a voice you have the right to use.

**The built-in voice is female** (measured pitch 219-267 Hz), so you get one without supplying a clip.

**Speed, measured on an RTX 4070 Laptop (8 GB), Turbo model, nothing else on the GPU:**

| What | Time |
|------|------|
| Model load, plus a warm-up generation, at server start | ~100 s (once) |
| A short reply: 2.0 s of speech | 5.6 s |
| A longer reply: 2.6 s of speech | 6.7 s |
| Roughly | 2 s of compute per 1 s of speech |

That is slower than ElevenLabs (about 1.5 s for the same sentence) and it is not the GPU's fault: the
card sits at ~55% and 28 W, because the model decodes one token at a time and Windows kernel-launch
overhead dominates. `torch.compile` does not currently help (CUDA graphs break on the KV cache).
If a reply needs to be spoken the instant it appears, keep ElevenLabs; if you want a free, private,
unlimited voice that can be cloned, keep Chatterbox. Both stay configured; `TTS_PROVIDER` switches.

**Long replies are spoken in pieces.** Chatterbox returns nothing until the whole clip is finished,
so a paragraph used to be twenty seconds of silence. `POST /api/voice/speak/plan` splits a reply into
sentence-sized pieces and the UI requests each one while the previous is playing. Measured on a
340-character reply this roughly halves the wait for the first word. It does **not** make playback
gapless: generation runs at about 0.42x realtime, so it cannot stay ahead of the audio and there is
a pause between sentences on a long reply. It starts sooner, which is the part that is felt.
Replies longer than 4000 characters are read up to there and left on screen.

**Markdown is never read out.** Everything the voice speaks goes through `strip_markdown()` in
`backend/app/emotion/speech.py`: `**bold**`, `` `code` ``, headings, bullets, tables, link URLs and
emoji are removed, a fenced code block becomes the sentence "The code is on screen.", and a bare URL
becomes "the link". Headings and bullets get a full stop so they do not run into the next line.

**Sounds it can actually make.** The tags Chatterbox performs were measured, not taken from the
documentation: synthesize `[tag] That is a shame...`, then transcribe the clip back with Whisper and
see whether the tag comes back as a word.

| Tag | Turbo | Multilingual |
|-----|-------|--------------|
| `[sigh]` `[gasp]` | performed | performed |
| `[chuckle]` `[laugh]` `[sniff]` `[cough]` | performed | **read out loud** ("Laugh. That is a shame...") |
| `[breath]` `[giggle]` `[whisper]` `[clears throat]` | **read out loud** | read out loud |

The two models genuinely differ, so the allowed set is keyed by model (`CHATTERBOX_SOUNDS` in
`backend/app/emotion/speech.py`, mirrored in the voice server). A sound the loaded model cannot
perform is dropped rather than spoken, and the language model is only told about cues it can make. Zeta uses a
sound at the start of a reply *sometimes* rather than always (25-60% depending on the mood) - the same
little sigh before every gentle answer sounds more synthetic than no sigh at all - and only on the
first piece of a streamed reply.

**Speaking Hindi: two voices, not one.** Understanding Hindi works today - faster-whisper is
multilingual (verified: `small` reports `is_multilingual = True`), so leave `STT_LANGUAGE` empty and
it detects the language per utterance. It used to be pinned to `en`, which forced English text out of
Hindi speech.

Speaking it is the harder half, and neither model is the answer on its own. Measured here:

Measured on the *same* 150-character English sentence with each model running alone - comparing
different sentences flatters the slower model, because fixed overheads dominate a short one:

| | Turbo | Multilingual |
|---|---|---|
| English | **0.42x realtime** (18-22 s) | 0.17x realtime (33-38 s) |
| Hindi | 16 s of nonsense ("Comeway. Comewood's lit-scar...") | correct - Whisper detects `hi` at p=1.00 |
| VRAM | ~2.0 GB | ~3.2 GB |
| Performs `[laugh]`/`[chuckle]` | yes | **no - reads them out as words** |

Multilingual costs about 2.4x turbo per second of speech, so making every English reply pay that to
get Hindi is a bad trade. Classifier-free guidance is not the cause, incidentally - `cfg_weight` 0.5,
0.3 and 0.0 all land within noise of each other. Neither model is fast enough to stay ahead of its own
playback; this is a choice between "slow" and "slower", not between smooth and stuttering.

So Zeta runs both and picks per reply, by the script the text is written in:

```
CHATTERBOX_MODEL=turbo                              # everyday voice, English
CHATTERBOX_NON_ENGLISH_MODEL=multilingual           # empty = off
CHATTERBOX_NON_ENGLISH_URL=http://127.0.0.1:8767
```

Only one holds the graphics card at a time - before speaking, the other is parked into system RAM and
comes back in 2-3 s, the same handover already used between the voice and the language model. The
second server is started lazily, the first time something non-English is actually said, so an
English-only day never loads it. `GET /api/system/status` shows "not started yet" until then; that is
normal, not a fault.

The wake word stays English (the phrase is "Hey Zeta"). Romanised Hinglish ("kya haal hai") reads as
English by script and is spoken by turbo, which handles it passably - better, at least, than waiting
50 seconds for the multilingual model to say the same sentence.

**Emotion.** Turbo ignores `exaggeration` and `cfg_weight`, so Zeta shapes its delivery through
`temperature` there (steadier when you sound low, livelier when you sound happy). The `base` and
`multilingual` models honour `exaggeration` and `cfg_weight` directly and Zeta uses those instead.
Only `turbo` has been exercised end to end here.

**VRAM: one card, two hungry models.** See "Sharing one GPU" below - on an 8 GB card this is the
single thing that decides whether Zeta feels instant or unusable.

**If Hugging Face is blocked on your network** (the model downloads from there on first run), set
`HF_ENDPOINT=https://hf-mirror.com` before starting the server. `HF_HUB_DISABLE_XET=1` also avoids a
Windows transfer bug.

## Sharing one GPU

Zeta wants the same graphics card three times: Ollama to think, Chatterbox to speak,
faster-whisper to listen. On a 24 GB desktop card this section does not matter. On an 8 GB
laptop card it decides whether Zeta answers in four seconds or four minutes.

The failure is silent, which is what makes it worth documenting. **Ollama does not report that a
model did not fit** - it moves the leftover layers to the CPU and carries on at roughly a tenth of
the speed. Everything looks configured correctly; replies just take minutes and eventually time out.

Measured on an RTX 4070 Laptop (8188 MB, ~720 MB of it taken by Windows and a live wallpaper):

| What | VRAM | Speed |
|---|---|---|
| `qwen3:8b` @ `num_ctx=16384` | 7450 MB - **1456 MB of it on the CPU** | replies took 180-228 s, then timed out |
| `qwen3:8b` @ `num_ctx=8192` | 5900 MB, all on the GPU | 39.7 tok/s; turns in 4-9 s |
| `qwen3:4b` @ `num_ctx=8192` | 3694 MB | 62.8 tok/s |
| Chatterbox Turbo, speaking | ~2000 MB | 0.4-0.5x realtime (5-8 s a sentence) |
| Chatterbox Turbo, speaking while the LLM holds the card | - | **0.1x realtime (57 s for a 5 s clip)** |
| Chatterbox Turbo on the CPU | 0 | 0.1x realtime (20 s a sentence) - not usable |
| faster-whisper `small` on the GPU | ~900 MB | 0.6 s an utterance |
| faster-whisper `small` on the CPU | 0 | 7.1 s an utterance |

Two conclusions from that table. **Nothing except Whisper is worth moving to the CPU** - the voice
is as slow there as it is when starved of VRAM, and the language model on the CPU *is* the bug. And
`LLM_CONTEXT_LENGTH` is the single most important setting on a small card: 16k costs 1550 MB more
than 8k, and that is exactly the margin that decides whether anything fits.

### How Zeta shares it

A reply is thought first and spoken afterwards, so the two big models never truly need the card at
the same instant. Zeta hands it over rather than letting either one spill:

* Before a turn, the voice is **parked**: its weights move to system RAM and the VRAM is released.
  Coming back costs about 4.6 s and happens while Zeta is already generating.
* Before speaking, the language model is **unloaded** (`keep_alive: 0`) if it would crowd the voice
  out. It reloads on the next turn.
* Never mid-turn: a task that is still running will want the model again in a moment.

`GPU_SHARE=auto` (the default) decides by measurement, not by guesswork: Zeta compares the model's
real footprint - from `/api/ps`, whatever model you configured - against the card, and only takes
turns when the two genuinely do not fit. Put a model on that leaves room for the voice and the
handover switches itself off, which the startup log says out loud:

```
qwen3:4b loaded: 3693 MB on the GPU, 168 MB VRAM free. The voice fits alongside it, so both stay loaded.
qwen3:8b does not fit on the GPU: 1456 MB of 7450 MB is running on the CPU, which makes replies
  roughly ten times slower. Free VRAM, or lower LLM_CONTEXT_LENGTH.
```

`GET /api/system/status` reports the same under `gpu`: total, free, whether Zeta is arbitrating, and
where the model's weights actually are. `CHATTERBOX_IDLE_UNLOAD` (default 300 s) is only a safety
net for a long silence - Zeta parks the voice itself before each turn when it needs to.

### Which model to run on 8 GB

`qwen3:8b` at 8k context is the default: it fits, and it answers cleanly. `qwen3:4b` is tempting -
2.2 GB smaller, 1.6x faster, and small enough that the handover switches off entirely - but as
measured here it calls tools correctly and then writes its reasoning out as the reply ("Okay, the
user is asking for the current time. Let me check the tools available…"), which Zeta would say out
loud. `think: false` does not suppress it. If you want to try a smaller brain, a non-thinking
instruct variant is the thing to test, not the hybrid.

## ElevenLabs models: speed vs expression

`ELEVENLABS_MODEL` decides how a reply is *performed*. All three work on a free key.

| Model | Delivery | Latency (measured, one short sentence) |
|-------|----------|----------------------------------------|
| `eleven_multilingual_v2` (default) | Natural and warm. Reads delivery tags aloud, so Zeta strips them | ~1.8 s |
| `eleven_flash_v2_5` | Fastest; slightly flatter | ~1.1 s |
| `eleven_v3` | **Performs** `[laughs]`, `[giggles]`, `[sighs]`, `[whispers]`, `[warmly]` | ~2.2 s |

Zeta only sends bracket tags to a v3 model and always strips them from the text you read. Emotional
delivery on the other models comes from `voice_settings` (stability/style), which Zeta adapts to how
you sound. A paid plan buys the voice *library* and higher limits, not the tags. See
[EMOTION.md](EMOTION.md).

## Streaming

`LLM_STREAM=true` (default) streams the reply to the UI while it is generated (`assistant_delta`
events over the WebSocket; the final `assistant_message` replaces the streamed text). Ollama,
OpenAI-compatible and Anthropic providers stream natively; others fall back to one chunk.
Model "thinking" (`<think>…</think>`) is never shown.

## Speech-to-text with ElevenLabs Scribe

`STT_PROVIDER=elevenlabs` uses ElevenLabs' Scribe model with the same `ELEVENLABS_API_KEY` (the key
needs the *Speech to Text* permission). It is noticeably more accurate than the small local Whisper
models for names ("Arnish", "Zeta") and accepts browser audio directly, so ffmpeg is not needed.
Cost: free tier includes a monthly allowance, then per-minute pricing. Local fallback: `STT_PROVIDER=whisper`.

## Opening files and folders by name, whole-drive access

`open_file` and `open_folder` accept a bare name ("AWS certification parts", "Games") and look it up
in the allowed folders: file index first, then a time-boxed walk of the drives. Matching is tolerant
of spaces vs underscores and small mis-hearings ("parts" finds `AWS_certification_paths.pdf`). One
clear best match opens directly; several equal matches are listed so Zeta can ask which one.
To let Zeta reach the whole machine set `FS_ALLOWED_ROOTS=C:\;D:\;E:\;F:\`. Reads work anywhere
under those roots; writes and deletes inside Windows/Program Files/AppData system folders stay blocked.
Index roots (`FS_INDEX_ROOTS`) can be narrower than allowed roots to keep the index fast.

## WhatsApp: sending by name

`send_whatsapp_message` takes the person's name. Resolution order: the local contact book
(`backend/config/contacts.json`, optional), then, with the WhatsApp Web provider, WhatsApp's own chat
search: Zeta types the name into WhatsApp Web's search box, opens the best matching chat (exact
title first), types the message and presses Enter. The confirmation dialog shows the chat that will
be used. If neither finds the person, Zeta asks for the phone number. The Business API and WAPI
providers need a phone number, so keep a contact book for those.

## OpenRouter (cloud models, one key)

```
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...        # https://openrouter.ai/keys
LLM_MODEL=openai/gpt-4o-mini        # or google/gemini-2.5-flash, anthropic/claude-sonnet-4, meta-llama/llama-3.3-70b-instruct
```

Or Settings, step 1, OpenRouter. Pick a model that supports tool calling (the ones above do); free
models end with `:free` and are rate-limited. Streaming, tool calls and vision work through the
OpenAI-compatible API. Costs are per token; gpt-4o-mini and gemini-flash class models are cents
per day for personal use. Zeta sends the model only the conversation and tool results, never keys.

## Performance (local models)

Where the time goes on a local model: the system prompt plus 71 tool schemas is ~6.6k tokens.
Re-reading ("prefill") that every turn on an RTX 4070 Laptop takes ~5 s. Zeta now keeps that prefix
byte-identical between turns (time and memories are attached to the user turn instead), so Ollama
reuses its prompt cache: measured 6.9 s for the first turn, 0.4 s for the next one.

More knobs, in order of impact:

1. **Keep the model fully on the GPU.** `qwen3:8b` with a 16k context needs ~7.5 GB; on an 8 GB
   card part of it spills to the CPU (Ollama showed 6.3 of 7.8 GB in VRAM), which slows every token.
   Set these Windows environment variables for Ollama and restart it: `OLLAMA_FLASH_ATTENTION=1`,
   `OLLAMA_KV_CACHE_TYPE=q8_0` (halves the context memory), or lower `LLM_CONTEXT_LENGTH` to 12288.
2. **Disable tool categories you don't use** in Settings, step 3. Fewer tools = shorter prompt.
3. `LLM_THINK=false` (default) - Qwen3's thinking mode doubles response time.
4. `LLM_MAX_TOKENS=1024` for shorter spoken replies.
5. A smaller model (`qwen3:4b`) is ~2x faster and still calls tools reliably for simple requests.
6. Or move the model to the cloud (OpenRouter above): first token in ~1 s, no VRAM limits.

Zeta also asks Ollama to keep the model loaded for 30 minutes between requests (`keep_alive`), so
the first request after a pause no longer pays the model-load time.
