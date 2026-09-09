import { useCallback, useEffect, useState } from "react";
import ActivityPanel from "./components/ActivityPanel";
import AuditPanel from "./components/AuditPanel";
import Chat from "./components/Chat";
import ConfirmationModal from "./components/ConfirmationModal";
import ConversationList from "./components/ConversationList";
import MemoryPanel from "./components/MemoryPanel";
import MonitorPanel from "./components/MonitorPanel";
import MoodPanel from "./components/MoodPanel";
import PlanView from "./components/PlanView";
import SchedulesPanel from "./components/SchedulesPanel";
import SetupWizard from "./components/SetupWizard";
import StatusPanel from "./components/StatusPanel";
import TasksPanel from "./components/TasksPanel";
import ToolPanel from "./components/ToolPanel";
import VoiceView from "./components/VoiceView";
import { useVoice } from "./hooks/useVoice";
import { useZeta } from "./hooks/useZeta";
import { api } from "./services/api";

type RightTab = "chat" | "activity" | "mood" | "tools" | "tasks" | "memory" | "schedules" | "audit" | "monitor";
const TABS: { id: RightTab; label: string; icon: string }[] = [
  { id: "chat", label: "Chat", icon: "▤" },
  { id: "activity", label: "Activity", icon: "◍" },
  { id: "mood", label: "Mood", icon: "♡" },
  { id: "tools", label: "Tools", icon: "⚙" },
  { id: "tasks", label: "Tasks", icon: "☰" },
  { id: "memory", label: "Memory", icon: "◈" },
  { id: "schedules", label: "Schedules", icon: "◷" },
  { id: "audit", label: "Audit", icon: "▣" },
  { id: "monitor", label: "Monitor", icon: "◉" },
];

function isEditable(el: EventTarget | null): boolean {
  const e = el as HTMLElement | null;
  if (!e) return false;
  const tag = e.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || e.isContentEditable;
}

export default function App() {
  const z = useZeta();
  const [leftOpen, setLeftOpen] = useState(false);
  const [rightOpen, setRightOpen] = useState(false);
  const [tab, setTab] = useState<RightTab>("chat");
  const [showSetup, setShowSetup] = useState<boolean | null>(null);

  const finalAssistant = z.messages.filter((m) => m.role === "assistant" && !m.interim && !m.streaming).slice(-1)[0];
  const lastUser = z.messages.filter((m) => m.role === "user").slice(-1)[0];

  const voice = useVoice({
    enabled: !!z.status?.voice?.stt_ok,
    ttsEnabled: !!z.status?.voice?.tts_ok,
    busy: z.busy,
    wakeSignal: z.wakeSignal,
    voiceMode: !(rightOpen && tab === "chat"),
    lastAssistant: finalAssistant,
    onTranscript: z.send,
  });

  const openRight = useCallback((t: RightTab) => {
    setTab(t);
    setRightOpen(true);
  }, []);

  useEffect(() => {
    api
      .setupStatus()
      .then((s) => setShowSetup(!s.setup_complete))
      .catch(() => setShowSetup(false));
  }, []);

  // Keyboard: Space = talk, Esc = stop / close drawers, [ ] = drawers, C = chat
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (showSetup || z.confirmations.length || isEditable(e.target)) return;
      if (e.code === "Space" && !e.repeat && !e.ctrlKey && !e.altKey && !e.metaKey) {
        e.preventDefault();
        if (voice.enabled) voice.toggle();
      } else if (e.key === "Escape") {
        if (voice.recording) voice.stop();
        else if (voice.speaking) voice.stopSpeaking();
        else if (z.busy) z.stop();
        else {
          setLeftOpen(false);
          setRightOpen(false);
        }
      } else if (e.key === "[") setLeftOpen((v) => !v);
      else if (e.key === "]") setRightOpen((v) => !v);
      else if (e.key.toLowerCase() === "c") openRight("chat");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [voice, z.busy, z.stop, z.confirmations.length, showSetup, openRight]);

  const pending = z.confirmations[0];
  const online = !!z.status && z.connected;
  const anyOpen = leftOpen || rightOpen;

  return (
    <div className={`app ${leftOpen ? "left-open" : ""} ${rightOpen ? "right-open" : ""}`}>
      <main className="main" onClick={() => anyOpen && (setLeftOpen(false), setRightOpen(false))}>
        <VoiceView voice={voice} busy={z.busy} status={z.status} lastUser={lastUser} emotion={z.emotion} onStop={z.stop} />
      </main>

      {/* edge controls */}
      <button className={`edge left ${leftOpen ? "on" : ""}`} onClick={() => setLeftOpen((v) => !v)} title="Status, conversations, settings  ( [ )">
        <span className={`dot ${online ? "ok pulse" : "bad"}`} />
        <span className="lines"><i /><i /><i /></span>
      </button>
      <button className={`edge right ${rightOpen ? "on" : ""}`} onClick={() => setRightOpen((v) => !v)} title="Chat, activity, tools…  ( ] )">
        {z.busy && <span className="busy-ring" />}
        <span className="glyph">▤</span>
        {z.messages.length > 0 && !rightOpen && <span className="count">{z.messages.length}</span>}
      </button>
      <div className="corner-actions">
        {voice.ttsEnabled && (
          <button className="btn ghost round" onClick={voice.toggleMute} title="Toggle spoken replies">{voice.muted ? "🔇" : "🔊"}</button>
        )}
        <button className="btn ghost round" onClick={() => setShowSetup(true)} title="Settings">⚙</button>
      </div>
      <div className="shortcuts"><kbd>Space</kbd> talk · <kbd>Esc</kbd> stop · <kbd>C</kbd> chat · <kbd>[</kbd> <kbd>]</kbd> panels</div>

      {/* drawers */}
      <aside className={`drawer left ${leftOpen ? "open" : ""}`}>
        <div className="drawer-body stagger">
          <StatusPanel status={z.status} connected={z.connected} onOpenSetup={() => setShowSetup(true)} onNewConversation={() => { z.newConversation(); openRight("chat"); }} />
          <ConversationList conversations={z.conversations} currentId={z.conversationId} onSelect={(id) => { z.selectConversation(id); openRight("chat"); }} onDelete={z.deleteConversation} />
          <PlanView task={z.currentTask} onCancel={(id) => api.cancelTask(id).catch(() => undefined)} />
        </div>
      </aside>

      <aside className={`drawer right ${rightOpen ? "open" : ""}`}>
        <nav className="rail">
          {TABS.map((t) => (
            <button key={t.id} className={`rail-btn ${tab === t.id ? "on" : ""}`} onClick={() => setTab(t.id)} title={t.label}>
              <span className="ic">{t.icon}</span>
              <span className="lb">{t.label}</span>
            </button>
          ))}
          <button className="rail-btn close" onClick={() => setRightOpen(false)} title="Close (Esc)"><span className="ic">✕</span></button>
        </nav>
        {rightOpen && <div className="drawer-body" key={tab}>
          {tab === "chat" && <Chat messages={z.messages} busy={z.busy} onSend={z.send} onStop={z.stop} status={z.status} voice={voice} />}
          {tab === "activity" && <ActivityPanel events={z.activity} onClear={() => z.setActivity([])} />}
          {tab === "mood" && <MoodPanel live={z.emotion} />}
          {tab === "tools" && <ToolPanel events={z.activity} />}
          {tab === "tasks" && <TasksPanel live={z.tasks} onSelectConversation={(id) => { z.selectConversation(id); setTab("chat"); }} />}
          {tab === "memory" && <MemoryPanel />}
          {tab === "schedules" && <SchedulesPanel />}
          {tab === "audit" && <AuditPanel />}
          {/* Mounted only while open: it polls the hardware every two seconds. */}
          {tab === "monitor" && <MonitorPanel />}
        </div>}
      </aside>

      {pending && <ConfirmationModal confirmation={pending} count={z.confirmations.length} onDecide={z.confirm} />}

      {showSetup && (
        <SetupWizard
          onClose={() => {
            setShowSetup(false);
            z.refreshStatus();
          }}
        />
      )}

      <div className="toasts">
        {z.notifications.map((n) => (
          <div key={n.id} className="toast" onClick={() => z.dismissNotification(n.id)}>
            <strong>{n.title}</strong>
            <div>{n.message}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
