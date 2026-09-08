import { useEffect, useRef, useState } from "react";
import type { VoiceState } from "../hooks/useVoice";
import type { ChatMessage, SystemStatus } from "../types";
import Markdown from "./Markdown";
import VoiceButton from "./VoiceButton";

interface Props {
  messages: ChatMessage[];
  busy: boolean;
  status: SystemStatus | null;
  voice: VoiceState;
  onSend: (text: string) => void;
  onStop: () => void;
}

const EXAMPLES = [
  "Find the PDF I edited yesterday and open it",
  "What's using the most memory right now?",
  "Open VS Code",
  "Check the current git branch in D:\\Projects\\Zeta",
  "Search the web for the latest AWS EC2 pricing",
  "Remember that I prefer concise answers",
];

export default function Chat({ messages, busy, status, voice, onSend, onStop }: Props) {
  const [text, setText] = useState("");
  const [speakingId, setSpeakingId] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    // Scroll only the message list (scrollIntoView would also drag hidden ancestors such as a closed drawer into view).
    const el = listRef.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [messages, busy]);

  useEffect(() => {
    if (!voice.speaking) setSpeakingId(null);
  }, [voice.speaking]);

  const submit = () => {
    if (!text.trim()) return;
    onSend(text);
    setText("");
    if (taRef.current) taRef.current.style.height = "48px";
  };

  const readAloud = async (m: ChatMessage) => {
    if (speakingId === m.id) {
      voice.stopSpeaking();
      setSpeakingId(null);
      return;
    }
    setSpeakingId(m.id);
    try {
      await voice.speak(m.content);
    } finally {
      setSpeakingId((id) => (id === m.id ? null : id));
    }
  };

  const wake = status?.voice?.wake;
  const streaming = messages.some((m) => m.streaming);

  return (
    <div className="chat">
      <div className="messages" ref={listRef}>
        {messages.length === 0 && (
          <div className="welcome fade-up">
            <h2>ZETA</h2>
            <div>Your AI operating layer. Tell me what you need.</div>
            {wake?.running && <div className="wake-hint">Wake word on: say “{wake.phrase}” to talk hands-free.</div>}
            <div className="examples">
              {EXAMPLES.map((e) => (
                <button key={e} onClick={() => onSend(e)}>
                  {e}
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.map((m) => (
          <div key={m.id} className={`msg ${m.role} ${m.interim ? "interim" : ""} ${m.streaming ? "streaming" : ""}`}>
            <div className="who">
              {m.role === "user" ? "You" : "Zeta"}
              {m.role === "assistant" && voice.ttsEnabled && !m.streaming && (
                <button className="speak" title={speakingId === m.id ? "Stop" : "Read aloud"} onClick={() => readAloud(m)}>
                  {speakingId === m.id ? "■" : "🔊"}
                </button>
              )}
            </div>
            {m.role === "assistant" ? <Markdown text={m.content} /> : <div className="plain">{m.content}</div>}
            {m.streaming && <span className="cursor">▍</span>}
          </div>
        ))}
        {busy && !streaming && (
          <div className="thinking">
            <span className="dots">
              <span />
              <span />
              <span />
            </span>
            Zeta is working…
          </div>
        )}
      </div>
      <div className="composer">
        <VoiceButton voice={voice} />
        <textarea
          ref={taRef}
          value={text}
          placeholder={voice.enabled ? "Type a message, or press Space to talk…" : "Type a message… (Enter to send, Shift+Enter for newline)"}
          onChange={(e) => {
            setText(e.target.value);
            e.target.style.height = "48px";
            e.target.style.height = Math.min(e.target.scrollHeight, 180) + "px";
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
        />
        {busy ? (
          <button className="stop" onClick={onStop} title="Cancel the running task (Esc)">
            ■ STOP
          </button>
        ) : (
          <button className="send" onClick={submit} disabled={!text.trim()}>
            SEND
          </button>
        )}
      </div>
    </div>
  );
}
