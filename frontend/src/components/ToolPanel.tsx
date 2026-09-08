import { useEffect, useState } from "react";
import { api } from "../services/api";
import type { ActivityEvent, ToolInfo } from "../types";

interface Props {
  events: ActivityEvent[];
}

/** Shows tools currently/recently used (from events) and the full catalogue with per-category switches. */
export default function ToolPanel({ events }: Props) {
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [enabled, setEnabled] = useState<Record<string, boolean>>({});
  const [showAll, setShowAll] = useState(false);

  const load = () => {
    api.tools().then(setTools).catch(() => undefined);
    api.permissions().then((p) => setEnabled(p.enabled)).catch(() => undefined);
  };
  useEffect(load, []);

  const recent = events.filter((e) => e.type === "tool_start" || e.type === "tool_end").slice(-30).reverse();
  const cats = Array.from(new Set(tools.map((t) => t.category))).sort();

  const toggle = async (cat: string) => {
    const next = { ...enabled, [cat]: !enabled[cat] };
    setEnabled(next);
    try {
      await api.updatePermissions({ enabled: { [cat]: next[cat] } });
      load();
    } catch {
      /* ignore */
    }
  };

  return (
    <div className="panel">
      <div className="panel-head">
        <h3 style={{ margin: 0 }}>{showAll ? "All tools" : "Tools in use"}</h3>
        <button className="btn sm" onClick={() => setShowAll(!showAll)}>{showAll ? "Recent" : "Catalogue"}</button>
      </div>
      <div style={{ overflow: "auto", flex: 1 }}>
        {!showAll && recent.length === 0 && <div className="empty">No tools used yet.</div>}
        {!showAll &&
          recent.map((e, i) => (
            <div key={i} className="toolcard">
              <div className="name">
                <span>{e.tool}</span>
                {e.risk && <span className={`risk ${e.risk}`}>{e.risk}</span>}
              </div>
              {e.type === "tool_start" && e.args && <div className="args">{JSON.stringify(e.args, null, 0).slice(0, 400)}</div>}
              {e.type === "tool_end" && <div className={`res ${e.success ? "ok" : "fail"}`}>{e.success ? "✓" : "✗"} {e.message}</div>}
            </div>
          ))}
        {showAll &&
          cats.map((cat) => (
            <div key={cat}>
              <div className="cat">
                <span>{cat}</span>
                <button className={`switch ${enabled[cat] !== false ? "on" : ""}`} onClick={() => toggle(cat)} title="Enable/disable this capability" />
              </div>
              {tools
                .filter((t) => t.category === cat)
                .map((t) => (
                  <div key={t.name} className={`toolcard ${t.enabled ? "" : "disabled"}`}>
                    <div className="name">
                      <span>{t.name}</span>
                      <span className={`risk ${t.risk_level}`}>{t.risk_level}{t.requires_confirmation ? " · CONFIRM" : ""}</span>
                    </div>
                    <div className="desc">{t.description}</div>
                  </div>
                ))}
            </div>
          ))}
      </div>
    </div>
  );
}
