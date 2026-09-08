import { useEffect, useState } from "react";
import { api } from "../services/api";
import type { MemoryItem } from "../types";

export default function MemoryPanel() {
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [text, setText] = useState("");
  const load = () => api.memory().then(setItems).catch(() => undefined);
  useEffect(() => {
    load();
    const iv = setInterval(load, 15000);
    return () => clearInterval(iv);
  }, []);

  const add = async () => {
    if (!text.trim()) return;
    await api.addMemory(text.trim(), "preference");
    setText("");
    load();
  };

  return (
    <div className="panel">
      <h3>Long-term memory</h3>
      <div className="memory-add">
        <input value={text} onChange={(e) => setText(e.target.value)} placeholder="Remember that…" onKeyDown={(e) => e.key === "Enter" && add()} />
        <button className="btn sm" onClick={add}>Add</button>
      </div>
      <div style={{ overflow: "auto", flex: 1 }}>
        {items.length === 0 && <div className="empty">Nothing remembered yet. Say "Remember that…" in chat.</div>}
        {items.map((m) => (
          <div key={m.id} className="memory-item">
            <div>
              <div className="c">{m.category}</div>
              {m.content}
            </div>
            <button className="btn sm danger" onClick={() => api.deleteMemory(m.id).then(load)} title="Forget">✕</button>
          </div>
        ))}
      </div>
    </div>
  );
}
