import { useEffect, useRef } from "react";
import type { ActivityEvent } from "../types";

interface Props {
  events: ActivityEvent[];
  onClear: () => void;
}

function fmt(ts: string) {
  try {
    return new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch {
    return "";
  }
}

const SHOWN = new Set(["activity", "tool_start", "tool_end", "task_update", "confirmation_required", "confirmation_resolved", "notification", "plan"]);

export default function ActivityPanel({ events, onClear }: Props) {
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [events]);

  const rows = events.filter((e) => SHOWN.has(e.type) && e.message);

  return (
    <div className="panel">
      <div className="panel-head">
        <h3 style={{ margin: 0 }}>Activity</h3>
        <button className="btn sm" onClick={onClear}>Clear</button>
      </div>
      <div className="log">
        {rows.length === 0 && <div className="empty">No activity yet.</div>}
        {rows.map((e, i) => (
          <div key={i} className={`row ${e.type} ${e.level || ""} ${e.type === "tool_end" ? (e.success ? "ok" : "fail") : ""}`}>
            <span className="t">{fmt(e.ts)}</span>
            <span className="m">
              {e.type === "tool_start" ? "▶ " : e.type === "tool_end" ? (e.success ? "✓ " : "✗ ") : e.type === "confirmation_required" ? "⚠ " : ""}
              {e.message}
              {e.type === "tool_end" && e.duration_ms != null ? ` (${e.duration_ms} ms)` : ""}
              {e.artifacts?.image_b64 && <img className="thumb" src={`data:image/png;base64,${e.artifacts.image_b64}`} alt="screenshot" />}
            </span>
          </div>
        ))}
        <div ref={endRef} />
      </div>
    </div>
  );
}
