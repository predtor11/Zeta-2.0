import { useEffect, useState } from "react";
import { api } from "../services/api";
import type { Schedule } from "../types";

function describe(s: Schedule) {
  if (s.schedule_type === "once") return s.run_at ? `once at ${new Date(s.run_at).toLocaleString()}` : "once";
  if (s.schedule_type === "interval") {
    const m = Math.round((s.interval_seconds || 0) / 60);
    return m >= 60 ? `every ${(m / 60).toFixed(m % 60 ? 1 : 0)} h` : `every ${m} min`;
  }
  return `cron ${s.cron}`;
}

export default function SchedulesPanel() {
  const [items, setItems] = useState<Schedule[]>([]);
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<"reminder" | "request">("reminder");
  const [payload, setPayload] = useState("");
  const [type, setType] = useState<"once" | "interval" | "cron">("once");
  const [runAt, setRunAt] = useState("");
  const [minutes, setMinutes] = useState("30");
  const [cron, setCron] = useState("0 9 * * 1-5");
  const [error, setError] = useState("");

  const load = () => api.schedules().then(setItems).catch(() => undefined);
  useEffect(() => {
    load();
    const iv = setInterval(load, 20000);
    return () => clearInterval(iv);
  }, []);

  const add = async () => {
    setError("");
    if (!payload.trim()) return setError("Say what to remind or run.");
    const body: Partial<Schedule> = { name: name.trim() || payload.trim().slice(0, 60), kind, payload: payload.trim(), schedule_type: type };
    if (type === "once") {
      if (!runAt) return setError("Pick a date and time.");
      body.run_at = new Date(runAt).toISOString();
    } else if (type === "interval") body.interval_seconds = Math.max(1, Number(minutes) || 30) * 60;
    else body.cron = cron.trim();
    try {
      await api.createSchedule(body);
      setAdding(false);
      setName("");
      setPayload("");
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Reminders & schedules</h3>
        <button className="btn sm" onClick={() => setAdding(!adding)}>{adding ? "Close" : "+ Add"}</button>
      </div>
      {adding && (
        <div className="sched-form">
          <div className="choice two">
            <button className={kind === "reminder" ? "on" : ""} onClick={() => setKind("reminder")}>Reminder<small>Notify me</small></button>
            <button className={kind === "request" ? "on" : ""} onClick={() => setKind("request")}>Request<small>Zeta runs it</small></button>
          </div>
          <div className="field">
            <input value={payload} onChange={(e) => setPayload(e.target.value)} placeholder={kind === "reminder" ? "Call the bank" : "Check my inbox and summarise new mail"} />
          </div>
          <div className="field">
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Name (optional)" />
          </div>
          <div className="choice three">
            {(["once", "interval", "cron"] as const).map((t) => (
              <button key={t} className={type === t ? "on" : ""} onClick={() => setType(t)}>{t}</button>
            ))}
          </div>
          <div className="field">
            {type === "once" && <input type="datetime-local" value={runAt} onChange={(e) => setRunAt(e.target.value)} />}
            {type === "interval" && <input type="number" min={1} value={minutes} onChange={(e) => setMinutes(e.target.value)} placeholder="Every N minutes" />}
            {type === "cron" && <input value={cron} onChange={(e) => setCron(e.target.value)} placeholder="min hour day month weekday" />}
            {type === "cron" && <div className="help">5-field cron, e.g. `0 9 * * 1-5` = weekdays at 09:00.</div>}
          </div>
          {error && <div className="help" style={{ color: "var(--err)" }}>{error}</div>}
          <button className="btn primary" onClick={add}>Save</button>
        </div>
      )}
      <div style={{ overflow: "auto", flex: 1 }}>
        {items.length === 0 && <div className="empty">Nothing scheduled. Try "Remind me at 6pm to call the bank".</div>}
        {items.map((s) => (
          <div key={s.id} className={`schedcard ${s.enabled ? "" : "off"}`}>
            <div className="row">
              <span className="n" title={s.payload}>{s.name}</span>
              <span className="chip">{s.kind}</span>
            </div>
            <div className="meta">
              {describe(s)}
              {s.next_run && s.enabled && ` · next ${new Date(s.next_run).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}`}
            </div>
            <div className="acts">
              <button className={`switch ${s.enabled ? "on" : ""}`} title={s.enabled ? "Disable" : "Enable"} onClick={() => api.enableSchedule(s.id, !s.enabled).then(load)} />
              <button className="btn sm danger" onClick={() => api.deleteSchedule(s.id).then(load)}>Delete</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
