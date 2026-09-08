import type { Conversation } from "../types";

interface Props {
  conversations: Conversation[];
  currentId: string | null;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
}

function when(iso: string) {
  const d = new Date(iso);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  return sameDay ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : d.toLocaleDateString([], { month: "short", day: "numeric" });
}

export default function ConversationList({ conversations, currentId, onSelect, onDelete }: Props) {
  return (
    <div className="convs">
      <h3>Conversations</h3>
      {conversations.length === 0 && <div className="empty">No conversations yet.</div>}
      <ul>
        {conversations.slice(0, 40).map((c) => (
          <li key={c.id} className={c.id === currentId ? "on" : ""} onClick={() => onSelect(c.id)} title={c.title}>
            <span className="t">{c.title || "New conversation"}</span>
            <span className="d">{when(c.updated_at)}</span>
            <button
              className="x"
              title="Delete conversation"
              onClick={(e) => {
                e.stopPropagation();
                onDelete(c.id);
              }}
            >
              ✕
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
