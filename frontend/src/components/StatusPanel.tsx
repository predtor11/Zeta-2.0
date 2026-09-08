import type { SystemStatus } from "../types";

interface Props {
  status: SystemStatus | null;
  connected: boolean;
  onOpenSetup: () => void;
  onNewConversation: () => void;
}

export default function StatusPanel({ status, connected, onOpenSetup, onNewConversation }: Props) {
  const online = !!status && connected;
  const llmOk = status?.llm.ok;
  const stt = status?.voice.stt || "disabled";
  const tts = status?.voice.tts || "disabled";
  const wake = status?.voice.wake;
  const db = status?.database;
  const voiceLabel = stt === "disabled" && tts === "disabled" ? "Disabled" : `${tts !== "disabled" ? tts : "-"} / ${stt !== "disabled" ? stt : "-"}`;
  const idx = status?.computer.index as { files?: number; running?: boolean } | undefined;

  return (
    <div className="status">
      <div className="brand">
        <h1>ZETA</h1>
        <span className="ver">v{status?.version || "…"}</span>
      </div>
      <div className={`online ${online ? "" : "off"}`}>
        <span className={`dot ${online ? "pulse" : ""}`} /> {online ? "ONLINE" : connected ? "STARTING" : "OFFLINE"}
      </div>
      <div className="kv">
        <span className="k">LLM</span>
        <span className={`v ${llmOk ? "ok" : "bad"}`} title={status?.llm.detail}>
          {status ? `${status.llm.provider} · ${status.llm.model}` : "…"}
        </span>
        <span className="k">Voice</span>
        <span className={`v ${stt === "disabled" && tts === "disabled" ? "warn" : status?.voice.tts_ok || status?.voice.stt_ok ? "ok" : "bad"}`} title={status?.voice.detail}>
          {voiceLabel}
        </span>
        {wake?.enabled && (
          <>
            <span className="k">Wake</span>
            <span className={`v ${wake.running ? "ok" : "bad"}`} title={wake.error || `${wake.engine} · ${wake.detections} detections`}>
              {wake.running ? `“${wake.phrase}”` : wake.error ? "error" : "starting"}
            </span>
          </>
        )}
        <span className="k">Internet</span>
        <span className={`v ${status?.internet.connected ? "ok" : "bad"}`}>{status?.internet.connected ? "Connected" : "Offline"}</span>
        <span className="k">Computer</span>
        <span className={`v ${status?.computer.connected ? "ok" : "warn"}`} title={idx ? `${idx.files ?? 0} files indexed` : ""}>
          {status?.computer.connected ? `Connected${idx?.running ? " · indexing" : idx?.files ? ` · ${idx.files} files` : ""}` : "Cloud mode"}
        </span>
        <span className="k">Database</span>
        <span className={`v ${db ? (db.ok ? "ok" : "bad") : ""}`} title={db ? `${db.location}${db.detail ? ` · ${db.detail}` : ""}` : ""}>
          {db ? (db.supabase ? "Supabase" : db.backend === "sqlite" ? "SQLite (local)" : db.backend) : "…"}
        </span>
        <span className="k">Tools</span>
        <span className="v">{status ? `${status.tools} across ${status.plugins.length} plugins` : "…"}</span>
      </div>
      <div className="actions">
        <button className="btn" onClick={onNewConversation}>+ New chat</button>
        <button className="btn" onClick={onOpenSetup}>Settings</button>
      </div>
    </div>
  );
}
