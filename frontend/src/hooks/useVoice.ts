import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../services/api";
import type { ChatMessage } from "../types";

export type VoicePhase = "off" | "idle" | "listening" | "transcribing" | "thinking" | "speaking";
export type ListenMode = "manual" | "auto";

export interface VoiceState {
  enabled: boolean;
  ttsEnabled: boolean;
  phase: VoicePhase;
  recording: boolean;
  mode: ListenMode;
  working: boolean;
  speaking: boolean;
  muted: boolean;
  error: string;
  lastHeard: string;
  /** Live audio levels (0..1) for animations; read inside requestAnimationFrame, no re-render. */
  levels: React.MutableRefObject<{ mic: number; out: number }>;
  start: (mode?: ListenMode) => Promise<void>;
  stop: () => void;
  toggle: () => void;
  toggleMute: () => void;
  stopSpeaking: () => void;
  speak: (text: string) => Promise<void>;
}

interface Options {
  enabled: boolean;            // STT configured
  ttsEnabled: boolean;         // TTS configured
  busy: boolean;               // a task is running
  wakeSignal: number;          // increments when the backend hears the wake word
  voiceMode: boolean;          // the voice view is active (auto-listen after questions even for typed input)
  lastAssistant?: ChatMessage; // latest final assistant reply
  onTranscript: (text: string) => void;
}

const QUESTION_RE = /\?\s*["'”’)]*\s*$/;

export function endsWithQuestion(text: string): boolean {
  const t = text.trim();
  if (!t) return false;
  if (QUESTION_RE.test(t)) return true;
  // last line / sentence is a question even if followed by a short remark in parentheses
  const last = t.split(/\n/).filter(Boolean).pop() || "";
  return /\?\s*$/.test(last.replace(/\(.*?\)\s*$/, "").trim());
}

/**
 * The voice engine shared by the voice view and the chat view:
 * microphone capture (with live level + optional silence auto-stop), transcription,
 * spoken replies with an output analyser, wake-word triggers and "listen again after a question".
 */
// Deafen the wake word listener for as long as Zeta is talking, so it never hears itself.
// Pauses only ever extend on the backend, so an over-estimate is free and an under-estimate is not.
const deafen = (seconds: number) => api.wakePause(Math.min(120, seconds + 2.5)).catch(() => undefined);

export function useVoice(o: Options): VoiceState {
  const [recording, setRecording] = useState(false);
  const [mode, setMode] = useState<ListenMode>("manual");
  const [working, setWorking] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const [error, setError] = useState("");
  const [lastHeard, setLastHeard] = useState("");
  const [muted, setMuted] = useState(() => {
    try {
      return localStorage.getItem("zeta_tts_muted") === "1";
    } catch {
      return false;
    }
  });

  const levels = useRef({ mic: 0, out: 0 });
  const ctxRef = useRef<AudioContext | null>(null);
  const recRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const micTimer = useRef<number>(0);
  const outTimer = useRef<number>(0);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const speechRef = useRef(0);                                   // bumped to cancel the reply being spoken
  const abortRef = useRef<AbortController | null>(null);
  const spokenRef = useRef<string | null>(null);
  const lastWake = useRef(o.wakeSignal);
  const lastInputVoice = useRef(false);
  const recordingRef = useRef(false);
  const workingRef = useRef(false);
  const optsRef = useRef(o);
  optsRef.current = o;

  const audioCtx = () => {
    if (!ctxRef.current) ctxRef.current = new AudioContext();
    if (ctxRef.current.state === "suspended") ctxRef.current.resume().catch(() => undefined);
    return ctxRef.current;
  };

  const stopSpeaking = useCallback(() => {
    speechRef.current += 1;        // anything still playing or being fetched belongs to an older reply
    abortRef.current?.abort();
    abortRef.current = null;
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current = null;
    }
    window.clearInterval(outTimer.current);
    levels.current.out = 0;
    setSpeaking(false);
  }, []);

  const stop = useCallback(() => {
    window.clearInterval(micTimer.current);
    levels.current.mic = 0;
    const rec = recRef.current;
    recRef.current = null;
    if (rec && rec.state !== "inactive") rec.stop();
    else streamRef.current?.getTracks().forEach((t) => t.stop());
    recordingRef.current = false;
    setRecording(false);
  }, []);

  const start = useCallback(
    async (m: ListenMode = "manual") => {
      if (recordingRef.current || workingRef.current || !optsRef.current.enabled) return;
      setError("");
      stopSpeaking();
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        streamRef.current = stream;
        const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus" : "audio/webm";
        const rec = new MediaRecorder(stream, { mimeType: mime });
        const chunks: Blob[] = [];
        let heardSpeech = false;
        rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
        rec.onstop = async () => {
          stream.getTracks().forEach((t) => t.stop());
          const blob = new Blob(chunks, { type: "audio/webm" });
          if (blob.size < 1000 || (m === "auto" && !heardSpeech)) {
            if (m === "auto") setError("I didn't hear anything.");
            return;
          }
          workingRef.current = true;
          setWorking(true);
          try {
            const { text } = await api.transcribe(blob);
            if (text.trim()) {
              setLastHeard(text.trim());
              lastInputVoice.current = true;
              optsRef.current.onTranscript(text.trim());
            } else setError("I didn't catch that.");
          } catch (e) {
            setError((e as Error).message);
          } finally {
            workingRef.current = false;
            setWorking(false);
          }
        };
        rec.start();
        recRef.current = rec;
        recordingRef.current = true;
        setRecording(true);
        setMode(m);
        api.wakePause(m === "auto" ? 25 : 70).catch(() => undefined);

        // Level meter (+ silence detection in auto mode)
        const ctx = audioCtx();
        const src = ctx.createMediaStreamSource(stream);
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 1024;
        src.connect(analyser);
        const buf = new Float32Array(analyser.fftSize);
        const started = Date.now();
        let lastVoice = Date.now();
        micTimer.current = window.setInterval(() => {
          analyser.getFloatTimeDomainData(buf);
          let sum = 0;
          for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
          const rms = Math.sqrt(sum / buf.length);
          levels.current.mic = Math.min(1, rms * 9);
          const now = Date.now();
          if (rms > 0.015) {
            heardSpeech = true;
            lastVoice = now;
          }
          const silent = now - lastVoice;
          if (m === "auto") {
            if ((heardSpeech && silent > 1500) || (!heardSpeech && now - started > 8000) || now - started > 30000) stop();
          } else if (now - started > 90000) stop();
        }, 60);
      } catch {
        setError("Microphone access denied.");
      }
    },
    [stop, stopSpeaking],
  );

  const playClip = useCallback(async (blob: Blob, live: () => boolean) => {
    const url = URL.createObjectURL(blob);
    const a = new Audio(url);
    audioRef.current = a;
    try {
      const ctx = audioCtx();
      const src = ctx.createMediaElementSource(a);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      src.connect(analyser);
      analyser.connect(ctx.destination);
      const buf = new Float32Array(analyser.fftSize);
      window.clearInterval(outTimer.current);
      outTimer.current = window.setInterval(() => {
        analyser.getFloatTimeDomainData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
        levels.current.out = Math.min(1, Math.sqrt(sum / buf.length) * 6);
      }, 50);
    } catch {
      /* analyser unavailable: play without levels */
    }
    // `duration` is only known once metadata has loaded; the caller has already deafened for a
    // rate estimate, this just tops it up with the real length.
    if (Number.isFinite(a.duration) && a.duration > 0) deafen(a.duration);
    else a.onloadedmetadata = () => Number.isFinite(a.duration) && deafen(a.duration);
    await new Promise<void>((resolve) => {
      a.onended = () => resolve();
      a.onerror = () => resolve();
      a.onpause = () => resolve();
      a.play().catch(() => resolve());
    });
    URL.revokeObjectURL(url);
    if (audioRef.current === a && live()) audioRef.current = null;
  }, []);

  // Speak a reply piece by piece.
  //
  // The voice returns nothing until a whole clip is finished, so a long answer used to be a long
  // silence - measured on this machine at roughly 40 ms per character, i.e. ~16 s before the first
  // word of a 400-character reply. The backend splits the reply into sentences; each piece is
  // requested while the previous one is playing, so the wait is only ever for the first sentence.
  const speak = useCallback(
    async (text: string) => {
      if (!optsRef.current.ttsEnabled || !text.trim()) return;
      stopSpeaking();
      const token = ++speechRef.current;
      const live = () => speechRef.current === token;

      let segments: string[] = [];
      try {
        segments = (await api.speakPlan(text)).segments;
      } catch {
        segments = [text];              // planning failed: speak it in one go, as before
      }
      if (!segments.length || !live()) return;

      const ac = new AbortController();
      abortRef.current = ac;
      const fetchPiece = (i: number) =>
        api
          .speak(segments[i], { lead: i === 0, final: i === segments.length - 1, signal: ac.signal })
          .catch(() => null);

      deafen(Math.min(90, 2 + text.length / 12));
      setSpeaking(true);
      let pending = fetchPiece(0);
      for (let i = 0; i < segments.length; i++) {
        const blob = await pending;
        pending = i + 1 < segments.length ? fetchPiece(i + 1) : Promise.resolve(null);
        if (!blob || !live()) break;
        await playClip(blob, live);
      }
      if (!live()) return;              // a newer reply took over; it owns the state now
      window.clearInterval(outTimer.current);
      levels.current.out = 0;
      abortRef.current = null;
      setSpeaking(false);
    },
    [stopSpeaking, playClip],
  );

  // New assistant reply: speak it, then listen again if it ended with a question.
  useEffect(() => {
    const m = o.lastAssistant;
    if (!m || m.id === spokenRef.current) return;
    spokenRef.current = m.id;
    if (!m.live) return; // history reload / conversation switch: never re-speak old replies
    const question = endsWithQuestion(m.content);
    const shouldListenAgain = () => question && optsRef.current.enabled && (optsRef.current.voiceMode || lastInputVoice.current);
    let cancelled = false;
    const run = async () => {
      if (o.ttsEnabled && !muted) {
        try {
          await speak(m.content);
        } catch {
          /* ignore */
        }
      }
      if (!cancelled && shouldListenAgain()) {
        setTimeout(() => !cancelled && start("auto"), 250);
      }
    };
    run();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [o.lastAssistant?.id]);

  // Wake word -> automatic capture
  useEffect(() => {
    if (o.wakeSignal === lastWake.current) return;
    lastWake.current = o.wakeSignal;
    if (o.enabled) start("auto");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [o.wakeSignal]);

  // Click / Space: start listening with end-of-speech detection; a second press sends early.
  const toggle = useCallback(() => {
    if (recordingRef.current) stop();
    else start("auto");
  }, [start, stop]);

  const toggleMute = useCallback(() => {
    setMuted((m) => {
      const v = !m;
      try {
        localStorage.setItem("zeta_tts_muted", v ? "1" : "0");
      } catch {
        /* ignore */
      }
      if (v) stopSpeaking();
      return v;
    });
  }, [stopSpeaking]);

  const phase: VoicePhase = !o.enabled && !o.ttsEnabled ? "off" : recording ? "listening" : working ? "transcribing" : speaking ? "speaking" : o.busy ? "thinking" : "idle";

  return { enabled: o.enabled, ttsEnabled: o.ttsEnabled, phase, recording, mode, working, speaking, muted, error, lastHeard, levels, start, stop, toggle, toggleMute, stopSpeaking, speak };
}
