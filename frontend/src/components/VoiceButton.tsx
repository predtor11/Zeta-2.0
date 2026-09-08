import type { VoiceState } from "../hooks/useVoice";

interface Props {
  voice: VoiceState;
}

/** Microphone button for the chat composer. All logic lives in useVoice. */
export default function VoiceButton({ voice }: Props) {
  const hint = voice.error
    || (voice.recording ? "LISTENING…"
      : voice.working ? "TRANSCRIBING"
        : voice.speaking ? "SPEAKING"
          : voice.enabled ? "VOICE · SPACE" : "VOICE OFF");
  return (
    <div className="voice-btn">
      <button
        className={`mic ${voice.recording ? "recording" : ""} ${voice.working ? "busy" : ""} ${voice.speaking ? "speaking" : ""}`}
        onClick={voice.toggle}
        disabled={!voice.enabled || voice.working}
        title={voice.enabled ? (voice.recording ? "Stop recording (Space)" : "Start recording (Space)") : "Voice input is disabled (set STT_PROVIDER in .env)"}
      >
        {voice.working ? "…" : voice.recording ? "■" : "🎤"}
        <span className="hint" title={hint}>{hint}</span>
      </button>
      {voice.ttsEnabled && (
        <button className="btn sm ghost" style={{ marginTop: 16 }} onClick={voice.toggleMute} title="Toggle spoken replies">
          {voice.muted ? "🔇" : "🔊"}
        </button>
      )}
    </div>
  );
}
