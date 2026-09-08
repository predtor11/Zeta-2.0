import { useEffect, useState } from "react";
import { api } from "../services/api";
import type { AuditEntry } from "../types";

const EVENTS = ["", "tool_call", "permission", "confirmation", "user_request", "error"];

export default function AuditPanel() {
  const [rows, setRows] = useState<AuditEntry[]>([]);
  const [event, setEvent] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const load = () => api.audit(150, event).then(setRows).catch(() => undefined);
  useEffect(() => {
    load();
    const iv = setInterval(load, 15000);
    return () => clearInterval(iv);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [event]);

  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Audit log</h3>
        <select className="sel" value={event} onChange={(e) => setEvent(e.target.value)}>
          {EVENTS.map((e) => <option key={e} value={e}>{e || "all events"}</option>)}
        </select>
      </div>
      <div className="log">
        {rows.length === 0 && <div className="empty">No audit entries yet.</div>}
        {rows.map((r) => (
          <div key={r.id} className={`row audit ${r.success === false || r.decision === "deny" || r.decision === "declined" ? "error" : ""}`} onClick={() => setOpen(open === r.id ? null : r.id)}>
            <span className="t">{r.ts ? new Date(r.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : ""}</span>
            <span className="m">
              <span className="ev">{r.event}</span>
              {r.tool && <span className="chip">{r.tool}</span>}
              {r.risk && <span className={`risk ${r.risk}`}>{r.risk}</span>}
              {r.decision && <span className="dec">{r.decision}</span>}
              {r.success === false && <span className="dec fail">failed</span>}
              {r.duration_ms != null && <span className="dur">{r.duration_ms} ms</span>}
              {open === r.id && r.details && <pre className="details">{JSON.stringify(r.details, null, 1)}</pre>}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
