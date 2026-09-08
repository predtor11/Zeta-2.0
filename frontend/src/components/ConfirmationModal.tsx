import { useEffect, useState } from "react";
import type { Confirmation } from "../types";

interface Props {
  confirmation: Confirmation;
  count: number;
  onDecide: (id: string, approved: boolean, remember: boolean) => void;
}

function renderDetails(details: Record<string, unknown>): string {
  const d = { ...details };
  delete d.description;
  const lines: string[] = [];
  for (const [k, v] of Object.entries(d)) {
    if (v == null || v === "" || (Array.isArray(v) && v.length === 0)) continue;
    if (Array.isArray(v) && v.every((x) => typeof x === "object")) {
      lines.push(`${k}:`);
      for (const item of v.slice(0, 40)) lines.push("  - " + Object.entries(item as Record<string, unknown>).map(([a, b]) => `${a}=${b}`).join(", "));
      if (v.length > 40) lines.push(`  … and ${v.length - 40} more`);
    } else if (typeof v === "object") {
      lines.push(`${k}: ${JSON.stringify(v)}`);
    } else {
      lines.push(`${k}: ${String(v)}`);
    }
  }
  return lines.join("\n");
}

export default function ConfirmationModal({ confirmation, count, onDecide }: Props) {
  const [remember, setRemember] = useState(false);
  const [left, setLeft] = useState(0);

  useEffect(() => {
    const tick = () => setLeft(Math.max(0, Math.round((new Date(confirmation.expires_at).getTime() - Date.now()) / 1000)));
    tick();
    const iv = setInterval(tick, 1000);
    return () => clearInterval(iv);
  }, [confirmation]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onDecide(confirmation.id, false, false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [confirmation, onDecide]);

  return (
    <div className="overlay">
      <div className="modal">
        <h2>ZETA WANTS TO PERFORM</h2>
        <div className="sub">
          Tool <code>{confirmation.tool}</code> · expires in {left}s{count > 1 ? ` · ${count - 1} more waiting` : ""}
        </div>
        <span className={`risk-tag ${confirmation.risk}`}>{confirmation.risk}</span>
        <div className="desc">{confirmation.description}</div>
        {Object.keys(confirmation.details || {}).length > 0 && <div className="details">{renderDetails(confirmation.details)}</div>}
        <div className="actions">
          <label>
            <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
            Don't ask again for this exact action (this session)
          </label>
          <button className="btn" onClick={() => onDecide(confirmation.id, false, false)}>Cancel</button>
          <button className="btn primary" onClick={() => onDecide(confirmation.id, true, remember)} autoFocus>
            Confirm
          </button>
        </div>
      </div>
    </div>
  );
}
