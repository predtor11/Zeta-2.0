import { useCallback, useEffect, useRef, useState } from "react";
import { api, wsUrl } from "../services/api";
import type { ActivityEvent, ChatMessage, Confirmation, Conversation, EmotionReading, EmotionTrend, SystemStatus, Task } from "../types";

const CONV_KEY = "zeta_conversation_id";
const TERMINAL = ["COMPLETED", "FAILED", "CANCELLED"];

function uid() {
  return Math.random().toString(36).slice(2, 10);
}

export function useZeta() {
  const [connected, setConnected] = useState(false);
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [activity, setActivity] = useState<ActivityEvent[]>([]);
  const [tasks, setTasks] = useState<Record<string, Task>>({});
  const [confirmations, setConfirmations] = useState<Confirmation[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [notifications, setNotifications] = useState<{ id: string; title: string; message: string }[]>([]);
  const [wakeSignal, setWakeSignal] = useState(0);
  const [emotion, setEmotion] = useState<EmotionReading | null>(null);
  const [moodTrend, setMoodTrend] = useState<EmotionTrend | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(() => {
    try {
      return localStorage.getItem(CONV_KEY);
    } catch {
      return null;
    }
  });
  const [busy, setBusy] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const convRef = useRef<string | null>(conversationId);
  convRef.current = conversationId;

  const refreshStatus = useCallback(async () => {
    try {
      setStatus(await api.status());
    } catch {
      setStatus((s) => (s ? { ...s, online: false } : s));
    }
  }, []);

  const refreshConversations = useCallback(() => api.conversations().then(setConversations).catch(() => undefined), []);

  const rememberConversation = (id: string | null) => {
    setConversationId(id);
    try {
      if (id) localStorage.setItem(CONV_KEY, id);
      else localStorage.removeItem(CONV_KEY);
    } catch {
      /* ignore */
    }
  };

  // Load history for the current conversation
  useEffect(() => {
    if (!conversationId) return;
    api
      .messages(conversationId)
      .then((rows) =>
        setMessages(
          rows.map((r) => ({ id: r.id, role: r.role as "user" | "assistant", content: r.content, taskId: r.task_id, createdAt: r.created_at })),
        ),
      )
      .catch(() => rememberConversation(null));
  }, [conversationId]);

  useEffect(() => {
    refreshStatus();
    refreshConversations();
    api.activity().then(setActivity).catch(() => undefined);
    api.confirmations().then(setConfirmations).catch(() => undefined);
    api.tasks(false).then((ts) => setTasks(Object.fromEntries(ts.map((t) => [t.id, t])))).catch(() => undefined);
    const iv = setInterval(refreshStatus, 20000);
    return () => clearInterval(iv);
  }, [refreshStatus, refreshConversations]);

  // WebSocket with reconnect
  useEffect(() => {
    let closed = false;
    let retry = 1000;
    const connect = () => {
      const ws = new WebSocket(wsUrl());
      wsRef.current = ws;
      ws.onopen = () => {
        setConnected(true);
        retry = 1000;
      };
      ws.onclose = () => {
        setConnected(false);
        if (!closed) setTimeout(connect, (retry = Math.min(retry * 2, 15000)));
      };
      ws.onmessage = (e) => {
        let ev: ActivityEvent & { pending_confirmations?: Confirmation[]; active_tasks?: Task[] };
        try {
          ev = JSON.parse(e.data);
        } catch {
          return;
        }
        handle(ev);
      };
    };
    const pushActivity = (ev: ActivityEvent) => setActivity((a) => [...a.slice(-400), ev]);
    const handle = (ev: ActivityEvent & { pending_confirmations?: Confirmation[]; active_tasks?: Task[] }) => {
      switch (ev.type) {
        case "hello":
          if (ev.pending_confirmations) setConfirmations(ev.pending_confirmations);
          if (ev.active_tasks) setTasks((ts) => ({ ...ts, ...Object.fromEntries(ev.active_tasks!.map((t) => [t.id, t])) }));
          break;
        case "activity":
        case "tool_start":
        case "tool_end":
        case "plan":
          pushActivity(ev);
          if (ev.type === "plan" && ev.task_id && ev.plan) {
            setTasks((ts) => (ts[ev.task_id!] ? { ...ts, [ev.task_id!]: { ...ts[ev.task_id!], plan: ev.plan! } } : ts));
          }
          break;
        case "task_update":
          if (ev.task) {
            const t = ev.task;
            setTasks((ts) => ({ ...ts, [t.id]: t }));
            pushActivity(ev);
            if (TERMINAL.includes(t.status)) {
              setBusy(false);
              refreshConversations();
              // drop any dangling streaming bubble for this task
              setMessages((m) => m.map((x) => (x.taskId === t.id && x.streaming ? { ...x, streaming: false } : x)));
            }
            if (t.conversation_id && !convRef.current) rememberConversation(t.conversation_id);
          }
          break;
        case "assistant_delta": {
          if (!ev.delta) break;
          const key = `${ev.task_id}:${ev.step ?? 0}`;
          setMessages((m) => {
            const idx = m.findIndex((x) => x.streamKey === key);
            if (idx >= 0) {
              const copy = m.slice();
              copy[idx] = { ...copy[idx], content: copy[idx].content + ev.delta };
              return copy;
            }
            return [...m, { id: uid(), role: "assistant", content: ev.delta!, taskId: ev.task_id, streaming: true, streamKey: key, createdAt: ev.ts }];
          });
          break;
        }
        case "assistant_stream_end": {
          const key = `${ev.task_id}:${ev.step ?? 0}`;
          setMessages((m) =>
            ev.discard ? m.filter((x) => x.streamKey !== key) : m.map((x) => (x.streamKey === key ? { ...x, streaming: false, interim: true } : x)),
          );
          break;
        }
        case "assistant_message":
          if (ev.content) {
            const content = ev.content;
            setMessages((m) => {
              // replace the streaming/interim bubble for the same task, else append
              let idx = -1;
              for (let i = m.length - 1; i >= 0; i--) {
                if (m[i].taskId === ev.task_id && (m[i].interim || m[i].streaming)) {
                  idx = i;
                  break;
                }
              }
              const msg: ChatMessage = { id: uid(), role: "assistant", content, taskId: ev.task_id, interim: !!ev.interim, live: true, createdAt: ev.ts };
              if (idx >= 0) {
                const copy = m.slice();
                copy[idx] = msg;
                return copy;
              }
              return [...m, msg];
            });
          }
          break;
        case "confirmation_required":
          if (ev.confirmation) setConfirmations((c) => [...c.filter((x) => x.id !== ev.confirmation!.id), ev.confirmation!]);
          pushActivity(ev);
          break;
        case "confirmation_resolved":
          setConfirmations((c) => c.filter((x) => x.id !== ev.confirmation_id));
          pushActivity(ev);
          break;
        case "notification":
          setNotifications((n) => [...n, { id: uid(), title: ev.title || "Zeta", message: ev.message || "" }]);
          pushActivity(ev);
          break;
        case "emotion":
          if (ev.emotion) setEmotion(ev.emotion);
          if (ev.trend) setMoodTrend(ev.trend);
          break;
        case "wake_word":
          setWakeSignal((n) => n + 1);
          pushActivity({ ...ev, type: "activity", message: `Wake word heard: ${ev.text || ev.phrase || ""}` });
          break;
        default:
          break;
      }
    };
    connect();
    return () => {
      closed = true;
      wsRef.current?.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const send = useCallback(async (text: string) => {
    const trimmed = text.trim();
    if (!trimmed) return;
    setMessages((m) => [...m, { id: uid(), role: "user", content: trimmed, createdAt: new Date().toISOString() }]);
    setBusy(true);
    try {
      const r = await api.chat(trimmed, convRef.current);
      if (r.conversation_id && r.conversation_id !== convRef.current) rememberConversation(r.conversation_id);
    } catch (e) {
      setBusy(false);
      setMessages((m) => [...m, { id: uid(), role: "assistant", content: `I couldn't reach the backend: ${(e as Error).message}`, createdAt: new Date().toISOString() }]);
    }
  }, []);

  const confirm = useCallback(async (id: string, approved: boolean, remember = false) => {
    setConfirmations((c) => c.filter((x) => x.id !== id));
    try {
      await api.confirm(id, approved, remember);
    } catch {
      /* already resolved */
    }
  }, []);

  const stop = useCallback(async () => {
    try {
      await api.cancelAll();
    } catch {
      /* ignore */
    }
    setBusy(false);
  }, []);

  const newConversation = useCallback(() => {
    rememberConversation(null);
    setMessages([]);
  }, []);

  const selectConversation = useCallback((id: string) => {
    if (id === convRef.current) return;
    setMessages([]);
    rememberConversation(id);
  }, []);

  const deleteConversation = useCallback(
    async (id: string) => {
      try {
        await api.deleteConversation(id);
      } catch {
        /* ignore */
      }
      if (id === convRef.current) {
        rememberConversation(null);
        setMessages([]);
      }
      refreshConversations();
    },
    [refreshConversations],
  );

  const dismissNotification = useCallback((id: string) => setNotifications((n) => n.filter((x) => x.id !== id)), []);

  const activeTasks = Object.values(tasks).filter((t) => !TERMINAL.includes(t.status));
  const currentTask =
    activeTasks.sort((a, b) => b.created_at.localeCompare(a.created_at))[0] ||
    Object.values(tasks).sort((a, b) => b.created_at.localeCompare(a.created_at))[0] ||
    null;

  return {
    connected, status, messages, activity, tasks, activeTasks, currentTask, confirmations, notifications, conversations, conversationId, busy, wakeSignal,
    emotion, moodTrend,
    send, confirm, stop, newConversation, selectConversation, deleteConversation, refreshStatus, refreshConversations, dismissNotification, setActivity,
  };
}
