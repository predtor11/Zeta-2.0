import type { Task } from "../types";

interface Props {
  task: Task | null;
  onCancel: (id: string) => void;
}

const ICON: Record<string, string> = { pending: "○", running: "◉", done: "✓", failed: "✗", skipped: "–" };

export default function PlanView({ task, onCancel }: Props) {
  const active = task && !["COMPLETED", "FAILED", "CANCELLED"].includes(task.status);
  return (
    <div className="plan">
      <h3>Current task</h3>
      {!task && <div className="empty">Idle. Ask Zeta to do something.</div>}
      {task && (
        <>
          <div className="task-head">
            <span className="req" title={task.request}>{task.request}</span>
            <span className={`badge ${task.status}`}>{task.status.replace(/_/g, " ")}</span>
          </div>
          {task.plan.length > 0 ? (
            <ul className="steps">
              {task.plan.map((s) => (
                <li key={s.index} className={s.status}>
                  <span className="ic">{ICON[s.status] || "○"}</span>
                  <span>
                    {s.description}
                    {s.note ? <span style={{ color: "var(--muted)" }}> — {s.note}</span> : null}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <div className="empty">{active ? "Working without a multi-step plan." : task.error ? `Error: ${task.error}` : "Finished."}</div>
          )}
          {task.tools_used.length > 0 && (
            <div className="tools-used">
              {task.tools_used.map((t) => (
                <span key={t} className="chip">{t}</span>
              ))}
            </div>
          )}
          {task.duration_ms != null && <div className="empty">Took {(task.duration_ms / 1000).toFixed(1)}s</div>}
          {active && (
            <button className="btn danger sm" style={{ marginTop: 10 }} onClick={() => onCancel(task.id)}>
              Cancel task
            </button>
          )}
        </>
      )}
    </div>
  );
}
