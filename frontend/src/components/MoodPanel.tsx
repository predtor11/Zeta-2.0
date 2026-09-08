import { useEffect, useState } from "react";
import { api } from "../services/api";
import { moodColor } from "../lib/mood";
import type { EmotionDay, EmotionReading, EmotionStatus } from "../types";

interface Props {
  live?: EmotionReading | null;
}

function pct(n: number) {
  return `${Math.round(n * 100)}%`;
}

function when(ts?: string | null) {
  if (!ts) return "";
  const d = new Date(ts);
  return isNaN(d.getTime()) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** Sparkline of average valence per day. Positive above the midline, negative below. */
function Sparkline({ days }: { days: EmotionDay[] }) {
  if (days.length < 2) return null;
  const w = 260;
  const h = 46;
  const step = w / (days.length - 1);
  const y = (v: number) => h / 2 - v * (h / 2 - 4);
  const pts = days.map((d, i) => `${(i * step).toFixed(1)},${y(d.valence).toFixed(1)}`).join(" ");
  return (
    <svg className="mood-spark" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" role="img" aria-label="Mood over time">
      <line x1="0" y1={h / 2} x2={w} y2={h / 2} className="mid" />
      <polyline points={pts} className="line" />
      {days.map((d, i) => (
        <circle key={d.day} cx={i * step} cy={y(d.valence)} r="2.5" fill={moodColor(d.valence, d.arousal)}>
          <title>{`${d.day} · ${d.mood} (${d.samples} readings)`}</title>
        </circle>
      ))}
    </svg>
  );
}

export default function MoodPanel({ live }: Props) {
  const [status, setStatus] = useState<EmotionStatus | null>(null);
  const [days, setDays] = useState<EmotionDay[]>([]);
  const [rows, setRows] = useState<EmotionReading[]>([]);
  const [err, setErr] = useState("");

  const load = () => {
    api.emotion().then(setStatus).catch((e) => setErr((e as Error).message));
    api.emotionDaily(14).then((d) => setDays(d.days)).catch(() => undefined);
    api.emotionHistory(40, 14).then(setRows).catch(() => undefined);
  };

  useEffect(() => {
    load();
    const iv = setInterval(load, 30000);
    return () => clearInterval(iv);
  }, []);

  // A new live reading arrives over the WebSocket; refresh the log behind it.
  useEffect(() => {
    if (live) load();
  }, [live?.at]);

  const current = live || status?.current || null;
  const trend = status?.trend;
  const enabled = status?.enabled !== false;
  const color = current ? moodColor(current.valence, current.arousal) : "var(--muted)";

  return (
    <div className="panel mood">
      <h3>Mood</h3>

      {err && <div className="empty">Couldn't read the mood log: {err}</div>}

      {!enabled && (
        <div className="empty">
          Emotional awareness is off. Set <code>EMOTION_ENABLED=true</code> in <code>.env</code> to turn it on.
        </div>
      )}

      {enabled && (
        <>
          <div className="mood-now" style={{ borderColor: color }}>
            <div className="ring" style={{ background: color, boxShadow: `0 0 26px ${color}` }} />
            <div className="txt">
              <div className="label">{current && current.confidence >= 0.2 ? current.label : "no clear reading yet"}</div>
              <div className="sub">
                {current && current.confidence >= 0.2
                  ? `${current.cues.slice(0, 3).join(" · ") || "from what you said"} · ${pct(current.confidence)} sure`
                  : "Say something and Zeta will read the room."}
              </div>
            </div>
          </div>

          {current && current.confidence >= 0.2 && (
            <div className="mood-bars">
              <div className="bar">
                <span className="k">Feeling</span>
                <span className="track">
                  <i className="mid" />
                  <i className="fill v" style={{ left: `${50 + Math.min(0, current.valence) * 50}%`, width: `${Math.abs(current.valence) * 50}%`, background: color }} />
                </span>
                <span className="v">{current.valence >= 0 ? "positive" : "negative"}</span>
              </div>
              <div className="bar">
                <span className="k">Energy</span>
                <span className="track">
                  <i className="fill" style={{ left: 0, width: pct(current.arousal), background: color }} />
                </span>
                <span className="v">{current.arousal >= 0.66 ? "high" : current.arousal >= 0.33 ? "medium" : "low"}</span>
              </div>
            </div>
          )}

          {current?.crisis && (
            <div className="mood-crisis">
              <strong>Support offered</strong>
              <div>Zeta noticed something serious and switched to a supportive reply. It is not a therapist. Real help:</div>
              <ul>
                {(current.crisis.resources || []).slice(0, 3).map((r) => (
                  <li key={r.name}>
                    {r.name} — <b>{r.contact}</b>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {trend && trend.samples > 0 && (
            <div className="mood-trend">
              This conversation: <b>{trend.direction}</b>
              {trend.dominant ? ` · mostly ${trend.dominant}` : ""} · {trend.samples} readings
            </div>
          )}

          {days.length > 1 && (
            <div className="mood-days">
              <div className="cap">Last {days.length} days</div>
              <Sparkline days={days} />
            </div>
          )}

          <div className="mood-log">
            <div className="cap">Recent readings</div>
            {rows.length === 0 && <div className="empty">Nothing logged yet.</div>}
            {rows.slice(0, 25).map((r, i) => (
              <div className="row" key={r.id ?? `${r.at}-${i}`}>
                <span className="pip" style={{ background: moodColor(r.valence, r.arousal) }} />
                <span className="lb">{r.label}</span>
                <span className="sn">{r.snippet || r.text || ""}</span>
                <span className="tm">{when(r.ts || r.at)}</span>
              </div>
            ))}
          </div>

          <div className="mood-foot">
            Read locally from your words{status?.prosody ? " and your tone of voice" : ""}. Never sent anywhere.
            {status?.expressive_voice ? " Voice can perform [laughs] and [sighs]." : ""}
          </div>
        </>
      )}
    </div>
  );
}
