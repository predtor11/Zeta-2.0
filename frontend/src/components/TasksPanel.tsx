import { useEffect, useState } from "react";
import { api } from "../services/api";
import type { Task } from "../types";

interface Props {
  live: Record<string, Task>;
  onSelectConversation: (id: string) => void;
}

const TERMINAL = ["COMPLETED", "FAILED", "CANCELLED"];

function fmt(ms: number | null) {
  if (ms == null) return "";
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export default function TasksPanel({ live, onSelectConversation }: Props) {
  const [history, setHistory] = useState<Task[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const load = () => api.taskHistory(60).then(setHistory).catch(() => undefined);
  useEffect(() => {
    load();
    const iv = setInterval(load, 20000);
    return () => clearInterval(iv);
  }, []);

  const merged: Record<string, Task> = {};
  for (const t of history) merged[t.id] = t;
  for (const t of Object.values(live)) merged[t.id] = t;
  const items = Object.values(merged).sort((a, b) => b.created_at.localeCompare(a.created_at));
  const active = items.filter((t) => !TERMINAL.includes(t.status));

  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Tasks {active.length > 0 && <span className="badge EXECUTING">{active.length} running</span>}</h3>
        <button className="btn sm" onClick={load}>Refresh</button>
      </div>
      <div style={{ overflow: "auto", flex: 1 }}>
        {items.length === 0 && <div className="empty">No tasks yet.</div>}
        {items.map((t) => (
          <div key={t.id} className={`taskcard ${open === t.id ? "open" : ""}`}>
            <div className="row" onClick={() => setOpen(open === t.id ? null : t.id)}>
              <span className="req" title={t.request}>{t.request}</span>
              <span className={`badge ${t.status}`}>{t.status.replace(/_/g, " ")}</span>
            </div>
            <div className="meta">
              {new Date(t.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
              {t.duration_ms != null && ` · ${fmt(t.duration_ms)}`}
              {t.tools_used.length > 0 && ` · ${t.tools_used.length} tool${t.tools_used.length > 1 ? "s" : ""}`}
              {!TERMINAL.includes(t.status) && (
                <button className="btn sm danger" style={{ marginLeft: 8 }} onClick={() => api.cancelTask(t.id).catch(() => undefined)}>
                  Cancel
                </button>
              )}
            </div>
            {open === t.id && (
              <div className="detail">
                {t.plan.length > 0 && (
                  <ul className="steps">
                    {t.plan.map((s) => (
                      <li key={s.index} className={s.status}>
                        <span className="ic">{s.status === "done" ? "✓" : s.status === "running" ? "●" : s.status === "failed" ? "✕" : "○"}</span>
                        <span>{s.description}</span>
                      </li>
                    ))}
                  </ul>
                )}
                {t.tools_used.length > 0 && <div className="tools-used">{t.tools_used.map((n) => <span key={n} className="chip">{n}</span>)}</div>}
                {t.result && <div className="result">{t.result.slice(0, 1200)}</div>}
                {t.error && t.status !== "COMPLETED" && <div className="result err">{t.error}</div>}
                {t.conversation_id && (
                  <button className="btn sm" style={{ marginTop: 6 }} onClick={() => onSelectConversation(t.conversation_id!)}>
                    Open conversation
                  </button>
                )}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
