import { useEffect, useState } from "react";
import { api, getToken, setToken } from "../services/api";
import type { SetupStatus } from "../types";
import IntegrationsPanel from "./IntegrationsPanel";

interface Props {
  onClose: () => void;
}

const PROVIDERS = [
  { id: "ollama", label: "Ollama", help: "Local models (qwen3, llama3.1…)" },
  { id: "lmstudio", label: "LM Studio", help: "Local OpenAI-compatible server" },
  { id: "openai", label: "OpenAI", help: "Cloud API key" },
  { id: "anthropic", label: "Anthropic", help: "Claude API key" },
  { id: "openrouter", label: "OpenRouter", help: "One key, many fast cloud models" },
  { id: "openai_compatible", label: "Other (OpenAI-compatible)", help: "Groq, OpenRouter, vLLM, llama.cpp…" },
];
const TTS = [
  { id: "chatterbox", label: "Chatterbox (local GPU)" },
  { id: "local", label: "Windows voices" },
  { id: "elevenlabs", label: "ElevenLabs" },
  { id: "disabled", label: "Disabled" },
];
const CHATTERBOX_MODELS = [
  { id: "turbo", label: "Turbo", help: "350M, English, built for voice agents. The fastest option on a GPU." },
  { id: "base", label: "Standard", help: "The original Chatterbox model. A little slower, slightly richer." },
  { id: "multilingual", label: "Multilingual", help: "500M, 23 languages including Hindi. Use this for Hinglish." },
];
const STT = [
  { id: "disabled", label: "Disabled" },
  { id: "whisper", label: "Local Whisper (faster-whisper)" },
  { id: "elevenlabs", label: "ElevenLabs Scribe (cloud)" },
  { id: "openai", label: "OpenAI-compatible STT" },
];
const PERMS = [
  { id: "filesystem", label: "Files", help: "Search, open, read and manage files in allowed folders" },
  { id: "computer", label: "Applications & system", help: "Launch/close apps, clipboard, screenshots, system info" },
  { id: "browser", label: "Browser & internet", help: "Web search, open sites, extract content" },
  { id: "terminal", label: "Terminal", help: "Run commands (sensitive/dangerous ones ask first)" },
  { id: "developer", label: "Developer tools", help: "Git, GitHub, Docker diagnostics" },
  { id: "messaging", label: "Messaging (WhatsApp)", help: "Sends always ask for confirmation" },
  { id: "email", label: "Email", help: "Read/search/send (sends ask first)" },
  { id: "screen", label: "Screen interaction", help: "Vision + click/type (asks first)" },
  { id: "scheduler", label: "Reminders & schedules", help: "Timed reminders and recurring requests" },
  { id: "memory", label: "Long-term memory", help: "Remember preferences and facts" },
];

const EL_MODELS = [
  { id: "eleven_multilingual_v2", label: "Natural", help: "Best all-round quality. Reads delivery tags aloud, so Zeta strips them." },
  { id: "eleven_flash_v2_5", label: "Fast", help: "Lowest latency, around half the delay. Slightly flatter delivery." },
  { id: "eleven_v3", label: "Expressive", help: "Performs [laughs], [sighs] and [whispers]. Works on the free tier; roughly a second slower." },
];

const REGIONS = [
  { id: "in", label: "India" },
  { id: "us", label: "US" },
  { id: "uk", label: "UK" },
  { id: "intl", label: "Other" },
];

type Current = {
  llm_provider?: string; llm_model?: string; tts_provider?: string; stt_provider?: string; allowed_roots?: string[];
  wake_word_enabled?: boolean; wake_word?: string; llm_stream?: boolean; tts_voice?: string; api_token_set?: boolean; database_backend?: string;
  emotion_enabled?: boolean; emotion_adapt_voice?: boolean; emotion_region?: string; elevenlabs_model?: string;
  chatterbox_voice?: string; chatterbox_model?: string; chatterbox_device?: string;
};

export default function SetupWizard({ onClose }: Props) {
  const [step, setStep] = useState(0);
  const [st, setSt] = useState<SetupStatus | null>(null);
  const [provider, setProvider] = useState("ollama");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [tts, setTts] = useState("disabled");
  const [ttsVoice, setTtsVoice] = useState("");
  const [stt, setStt] = useState("disabled");
  const [elKey, setElKey] = useState("");
  const [wake, setWake] = useState(false);
  const [emotion, setEmotion] = useState(true);
  const [adaptVoice, setAdaptVoice] = useState(true);
  const [region, setRegion] = useState("in");
  const [elModel, setElModel] = useState("eleven_multilingual_v2");
  const [cbModel, setCbModel] = useState("turbo");
  const [cbVoice, setCbVoice] = useState("");
  const [cbDevice, setCbDevice] = useState("auto");
  const [cbVoices, setCbVoices] = useState<{ id: string; name: string }[]>([]);
  const [cbHealth, setCbHealth] = useState<{ ok?: boolean; detail?: string } | null>(null);
  const [wakePhrase, setWakePhrase] = useState("hey zeta");
  const [stream, setStream] = useState(true);
  const [perms, setPerms] = useState<Record<string, boolean>>({ filesystem: true, computer: true, browser: true, developer: true, scheduler: true, memory: true, terminal: false, messaging: false, email: false, screen: false });
  const [roots, setRoots] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [apiToken, setApiToken] = useState(getToken());
  const [apiTokenTouched, setApiTokenTouched] = useState(false);
  const [dbBackend, setDbBackend] = useState("sqlite");
  const [dbUrl, setDbUrl] = useState("");
  const [dbTouched, setDbTouched] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .setupStatus()
      .then((s) => {
        setSt(s);
        const cur = s.current as Current;
        if (s.setup_complete) {
          setProvider(cur.llm_provider || "ollama");
          setModel(cur.llm_model || "");
          setTts(cur.tts_provider || "disabled");
          setStt(cur.stt_provider || "disabled");
          setTtsVoice(cur.tts_voice || "");
          setWake(!!cur.wake_word_enabled);
          setEmotion(cur.emotion_enabled !== false);
          setAdaptVoice(cur.emotion_adapt_voice !== false);
          setRegion(cur.emotion_region || "in");
          setElModel(cur.elevenlabs_model || "eleven_multilingual_v2");
          setCbModel(cur.chatterbox_model || "turbo");
          setCbVoice(cur.chatterbox_voice || "");
          setCbDevice(cur.chatterbox_device || "auto");
          setWakePhrase(cur.wake_word || "hey zeta");
          setStream(cur.llm_stream !== false);
          setDbBackend(cur.database_backend || "sqlite");
          setRoots((cur.allowed_roots || []).join(";"));
          if (s.permissions?.enabled) setPerms((p) => ({ ...p, ...s.permissions.enabled }));
        } else if (!s.ollama.available && s.lmstudio.available) {
          setProvider("lmstudio");
        }
        if (!cur.llm_model && s.ollama.models?.length) setModel(s.ollama.models[0]);
      })
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    if (tts !== "chatterbox") return;
    api.voices().then((v) => setCbVoices(v.voices || [])).catch(() => undefined);
    api.status().then((s) => setCbHealth({ ok: s.voice?.tts_ok, detail: s.voice?.detail || "" })).catch(() => undefined);
  }, [tts]);

  const models = provider === "ollama" ? st?.ollama.models || [] : provider === "lmstudio" ? st?.lmstudio.models || [] : [];

  const save = async () => {
    setSaving(true);
    setError("");
    try {
      const body: Record<string, unknown> = {
        llm_provider: provider, llm_model: model, llm_base_url: baseUrl, llm_api_key: apiKey, tts_provider: tts, stt_provider: stt,
        elevenlabs_api_key: elKey, tts_voice: ttsVoice, wake_word_enabled: wake, wake_word: wakePhrase, llm_stream: stream,
        emotion_enabled: emotion, emotion_adapt_voice: adaptVoice, emotion_region: region, elevenlabs_model: elModel,
        chatterbox_model: cbModel, chatterbox_voice: cbVoice, chatterbox_device: cbDevice,
        permissions: perms, allowed_roots: roots.split(";").map((r) => r.trim()).filter(Boolean),
      };
      if (dbTouched) body.database_url = dbUrl.trim();
      if (apiTokenTouched) {
        body.api_token = apiToken.trim();
        setToken(apiToken.trim());
      }
      await api.setup(body);
      setStep(3);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="overlay">
      <div className="modal wide wizard">
        <h2>ZETA SETUP</h2>
        <div className="sub">{st?.setup_complete ? "Update your configuration." : "First run: choose your AI provider, voice, what Zeta may access, and connect accounts."}</div>
        <div className="steps-nav">{[0, 1, 2, 3].map((i) => <span key={i} className={i <= step ? "on" : ""} />)}</div>

        {step === 0 && (
          <>
            <div className="choice">
              {PROVIDERS.map((p) => (
                <button key={p.id} className={provider === p.id ? "on" : ""} onClick={() => setProvider(p.id)}>
                  {p.label}
                  <small>
                    {p.help}
                    {p.id === "ollama" && st ? (st.ollama.available ? " · detected ✓" : " · not running") : ""}
                    {p.id === "lmstudio" && st ? (st.lmstudio.available ? " · detected ✓" : " · not running") : ""}
                  </small>
                </button>
              ))}
            </div>
            <div className="field">
              <label>Model</label>
              {models.length > 0 ? (
                <select value={model} onChange={(e) => setModel(e.target.value)}>
                  {!models.includes(model) && model && <option value={model}>{model}</option>}
                  {models.map((m) => <option key={m} value={m}>{m}</option>)}
                </select>
              ) : (
                <input value={model} onChange={(e) => setModel(e.target.value)} placeholder={provider === "openai" ? "gpt-4o-mini" : provider === "anthropic" ? "claude-sonnet-5" : provider === "openrouter" ? "openai/gpt-4o-mini" : "qwen3:8b"} />
              )}
              <div className="help">
                {provider === "ollama" && !st?.ollama.available && "Ollama isn't reachable. Install it from ollama.com, then run `ollama pull qwen3:8b`."}
                {provider === "ollama" && st?.ollama.available && models.length === 0 && "Ollama is running but has no models. Run `ollama pull qwen3:8b`."}
                {provider === "ollama" && "For reliable tool use pick a model that supports tools (qwen3, llama3.1, qwen2.5, mistral-nemo)."}
                {provider === "openrouter" && "Fast, tool-capable picks: openai/gpt-4o-mini, google/gemini-2.5-flash, anthropic/claude-sonnet-4, meta-llama/llama-3.3-70b-instruct. Free models end with :free."}
              </div>
            </div>
            {(provider === "openai_compatible" || provider === "lmstudio") && (
              <div className="field">
                <label>Base URL</label>
                <input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder={provider === "lmstudio" ? "http://localhost:1234/v1" : "https://api.groq.com/openai/v1"} />
              </div>
            )}
            {(provider === "openai" || provider === "anthropic" || provider === "openai_compatible" || provider === "openrouter") && (
              <div className="field">
                <label>API key</label>
                <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="Stored in .env only — never sent to the model" />
              </div>
            )}
            <div className="perm-row">
              <div>
                Stream replies
                <small>Show the answer word by word as it is generated</small>
              </div>
              <button className={`switch ${stream ? "on" : ""}`} onClick={() => setStream(!stream)} />
            </div>
          </>
        )}

        {step === 1 && (
          <>
            <div className="field">
              <label>Voice output</label>
              <div className="choice">
                {TTS.map((t) => <button key={t.id} className={tts === t.id ? "on" : ""} onClick={() => setTts(t.id)}>{t.label}</button>)}
              </div>
              {tts === "chatterbox" && (
                <>
                  <div className="help">
                    Zeta's own voice, generated on your GPU. No key, no quota, nothing leaves the machine.
                    Install it once with <code>install_tts.bat</code>; the voice server then starts by itself.
                  </div>
                  <div className="choice" style={{ marginTop: 8 }}>
                    {CHATTERBOX_MODELS.map((m) => (
                      <button key={m.id} className={cbModel === m.id ? "on" : ""} onClick={() => setCbModel(m.id)} title={m.help}>{m.label}</button>
                    ))}
                  </div>
                  <div className="help">{CHATTERBOX_MODELS.find((m) => m.id === cbModel)?.help}</div>
                  <input style={{ marginTop: 8 }} value={cbVoice} onChange={(e) => setCbVoice(e.target.value)}
                         placeholder="Reference clip, e.g. voice/zeta_female.wav — empty uses the built-in voice" />
                  <div className="help">
                    Drop a clean 7-15 second WAV of the voice you want into the <code>voice</code> folder.
                    {cbVoices.length > 0 && <> Found: {cbVoices.map((v) => v.id).join(", ")}.</>}
                  </div>
                  <div className="choice" style={{ marginTop: 8 }}>
                    {["auto", "cuda", "cpu"].map((d) => (
                      <button key={d} className={cbDevice === d ? "on" : ""} onClick={() => setCbDevice(d)}>{d === "auto" ? "Auto" : d.toUpperCase()}</button>
                    ))}
                  </div>
                  {cbHealth && (
                    <div className="help">Voice server: {cbHealth.ok ? "ready" : "not running"} — {cbHealth.detail}</div>
                  )}
                </>
              )}
              {tts === "elevenlabs" && (
                <>
                  <input type="password" value={elKey} onChange={(e) => setElKey(e.target.value)} placeholder="ElevenLabs API key (sk_…) — leave blank to keep the saved one" />
                  <input style={{ marginTop: 6 }} value={ttsVoice} onChange={(e) => setTtsVoice(e.target.value)} placeholder="Voice: george, daniel, sarah, brian, alice, lily… or a voice id" />
                  <div className="help">Free accounts can use ElevenLabs' premade voices; library voices need a paid plan.</div>
                  <div className="choice" style={{ marginTop: 8 }}>
                    {EL_MODELS.map((m) => (
                      <button key={m.id} className={elModel === m.id ? "on" : ""} onClick={() => setElModel(m.id)} title={m.help}>{m.label}</button>
                    ))}
                  </div>
                  <div className="help">{EL_MODELS.find((m) => m.id === elModel)?.help}</div>
                </>
              )}
              {tts === "local" && <input value={ttsVoice} onChange={(e) => setTtsVoice(e.target.value)} placeholder="Windows voice name (optional), e.g. Microsoft Zira Desktop" />}
            </div>
            <div className="field">
              <label>Voice input</label>
              <div className="choice">
                {STT.map((t) => <button key={t.id} className={stt === t.id ? "on" : ""} onClick={() => setStt(t.id)}>{t.label}</button>)}
              </div>
              <div className="help">{stt === "whisper" && "Runs locally with faster-whisper (CPU works; GPU needs the CUDA libraries, see .env.example)."}</div>
            </div>
            <div className="perm-row">
              <div>
                Wake word (hands-free)
                <small>Zeta listens on the microphone and starts a voice request when it hears the phrase. Needs voice input on and `pip install sounddevice numpy`.</small>
              </div>
              <button className={`switch ${wake ? "on" : ""}`} onClick={() => setWake(!wake)} disabled={stt === "disabled"} />
            </div>
            {wake && (
              <div className="field">
                <label>Wake phrase</label>
                <input value={wakePhrase} onChange={(e) => setWakePhrase(e.target.value)} placeholder="hey zeta" />
              </div>
            )}
            <div className="perm-row">
              <div>
                Emotional awareness
                <small>Zeta reads how you seem from your words and tone of voice, and answers accordingly. All of it stays on this machine.</small>
              </div>
              <button className={`switch ${emotion ? "on" : ""}`} onClick={() => setEmotion(!emotion)} />
            </div>
            {emotion && (
              <>
                <div className="perm-row">
                  <div>
                    Match the spoken delivery
                    <small>Softer and slower when you sound low, brighter when you sound happy.</small>
                  </div>
                  <button className={`switch ${adaptVoice ? "on" : ""}`} onClick={() => setAdaptVoice(!adaptVoice)} disabled={tts === "disabled"} />
                </div>
                <div className="field">
                  <label>Helplines to offer if someone is in real distress</label>
                  <div className="choice">
                    {REGIONS.map((r) => (
                      <button key={r.id} className={region === r.id ? "on" : ""} onClick={() => setRegion(r.id)}>{r.label}</button>
                    ))}
                  </div>
                  <div className="help">Zeta is not a therapist and never pretends to be. It stays with you and points once to real human help.</div>
                </div>
              </>
            )}
          </>
        )}

        {step === 2 && (
          <>
            {PERMS.map((p) => (
              <div key={p.id} className="perm-row">
                <div>
                  {p.label}
                  <small>{p.help}</small>
                </div>
                <button className={`switch ${perms[p.id] ? "on" : ""}`} onClick={() => setPerms({ ...perms, [p.id]: !perms[p.id] })} />
              </div>
            ))}
            <div className="field" style={{ marginTop: 12 }}>
              <label>Folders Zeta may access (semicolon-separated)</label>
              <input value={roots} onChange={(e) => setRoots(e.target.value)} placeholder="Default: your home folder — e.g. C:\Users\you;D:\Projects" />
              <div className="help">Dangerous actions (deleting, shutting down, sending messages) always ask for confirmation regardless of these switches.</div>
            </div>
            <button className="btn sm" onClick={() => setAdvanced(!advanced)}>{advanced ? "Hide advanced" : "Advanced: API token, database"}</button>
            {advanced && (
              <div className="advanced">
                <div className="field">
                  <label>API token {(st?.current as Current)?.api_token_set ? "(set)" : "(not set)"}</label>
                  <input
                    type="password"
                    value={apiToken}
                    onChange={(e) => {
                      setApiToken(e.target.value);
                      setApiTokenTouched(true);
                    }}
                    placeholder="Required only when Zeta is reachable from other machines (cloud mode)"
                  />
                  <div className="help">Saved to .env as API_TOKEN and remembered by this browser. Leave empty for localhost-only use.</div>
                </div>
                <div className="field">
                  <label>Database — currently {dbBackend === "sqlite" ? "local SQLite (data/zeta.db)" : "PostgreSQL"}</label>
                  <input
                    value={dbUrl}
                    onChange={(e) => {
                      setDbUrl(e.target.value);
                      setDbTouched(true);
                    }}
                    placeholder="postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres"
                  />
                  <div className="help">
                    Optional Supabase/PostgreSQL for conversations, tasks, memory, audit and schedules (`pip install asyncpg`). Existing data can be copied with `python scripts/migrate_db.py --to "…"`.
                    {dbBackend !== "sqlite" && (
                      <>
                        {" "}
                        <a href="#" onClick={(e) => { e.preventDefault(); setDbUrl(""); setDbTouched(true); }}>Switch back to local SQLite</a>
                      </>
                    )}
                  </div>
                </div>
              </div>
            )}
          </>
        )}

        {step === 3 && <IntegrationsPanel />}

        {error && <div style={{ color: "var(--err)", marginTop: 8 }}>{error}</div>}
        <div className="actions">
          {st?.setup_complete && <button className="btn" onClick={onClose}>Close</button>}
          {step > 0 && <button className="btn" onClick={() => setStep(step - 1)}>Back</button>}
          {step < 2 && <button className="btn primary" onClick={() => setStep(step + 1)}>Next</button>}
          {step === 2 && <button className="btn primary" onClick={save} disabled={saving}>{saving ? "Saving…" : "Save & continue"}</button>}
          {step === 3 && <button className="btn primary" onClick={onClose}>Done</button>}
        </div>
      </div>
    </div>
  );
}
