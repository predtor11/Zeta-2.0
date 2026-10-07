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

const JOIN_GAP = 0.16;          // fallback pause between pieces when the backend did not send one
const SAFETY = 1.06;            // the estimates wobble by a few per cent; buy that much room

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
  const outRef = useRef<{ gain: GainNode; analyser: AnalyserNode } | null>(null);
  const sourcesRef = useRef<AudioBufferSourceNode[]>([]);   // pieces scheduled on the audio clock
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
    // Pieces are scheduled ahead of time, so silencing the voice means stopping everything that
    // has been queued, not only whatever happens to be audible.
    for (const src of sourcesRef.current) {
      try {
        src.stop();
      } catch {
        /* already finished */
      }
    }
    sourcesRef.current = [];
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

  // One output chain for the whole session: every piece is connected to the same gain node, so
  // the level meter does not have to be rebuilt (and `createMediaElementSource`, which cannot be
  // called twice on the same element, is out of the picture entirely).
  const output = () => {
    const ctx = audioCtx();
    if (!outRef.current) {
      const gain = ctx.createGain();
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      gain.connect(analyser);
      analyser.connect(ctx.destination);
      outRef.current = { gain, analyser };
    }
    return outRef.current;
  };

  const meter = (analyser: AnalyserNode) => {
    const buf = new Float32Array(analyser.fftSize);
    window.clearInterval(outTimer.current);
    outTimer.current = window.setInterval(() => {
      analyser.getFloatTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
      levels.current.out = Math.min(1, Math.sqrt(sum / buf.length) * 6);
    }, 50);
  };

  // Speak a reply.
  //
  // The one fact that decides how this works: the voice generates speech more slowly than it is
  // spoken - about 0.42x realtime on this card, so a second of speech costs roughly 2.4 seconds
  // of work. Requesting a sentence, playing it, and requesting the next therefore runs dry at
  // every join, and the gap grows with the length of the sentence before it. That was the
  // stuttering.
  //
  // So the pieces are generated in order but not played on arrival. Playback starts only once
  // every piece still to come will be *finished* before playback reaches it - which is not the
  // same as "generation keeps up on average", because half a piece is silence. So the check walks
  // the pieces that are left, pricing each from what the pieces already made actually cost, and
  // compares when each one lands against when it would be needed. Treating generation as a
  // steady stream instead says "safe" and then delivers the last piece five seconds late.
  //
  // Nothing here is a constant to be tuned: the per-character cost of speech and of work are both
  // measured as it goes, so a faster machine simply starts sooner, and one that can outrun
  // playback starts on the first piece.
  //
  // Everything in hand is then scheduled end to end on the audio clock, which joins the pieces
  // sample-accurately instead of hoping a new <audio> element starts the instant the last ended.
  const speak = useCallback(
    async (text: string) => {
      if (!optsRef.current.ttsEnabled || !text.trim()) return;
      stopSpeaking();
      const token = ++speechRef.current;
      const live = () => speechRef.current === token;

      let segments: string[] = [];
      let gaps: number[] = [];
      try {
        const plan = await api.speakPlan(text);
        segments = plan.segments;
        gaps = plan.gaps || [];
      } catch {
        segments = [text];              // planning failed: speak it in one go, as before
      }
      if (!segments.length || !live()) return;

      const ac = new AbortController();
      abortRef.current = ac;
      const ctx = audioCtx();
      // Pieces are scheduled against `ctx.currentTime`, which does not advance while the context
      // is suspended - so unlike an <audio> element, this has to be awake before anything is
      // queued, not merely asked to wake up.
      if (ctx.state !== "running") await ctx.resume().catch(() => undefined);
      const { gain, analyser } = output();
      setSpeaking(true);

      const ready: { buf: AudioBuffer; gap: number }[] = [];
      let madeAudio = 0;                // seconds of speech generated so far
      let madeChars = 0;                // ...how many characters that was
      let spentWall = 0;                // ...and how long it took
      let allMade = false;
      let queued = 0;                   // how many of `ready` are already on the audio clock
      let started = false;
      let nextStart = 0;                // audio-clock time the next piece begins
      let last: AudioBufferSourceNode | null = null;

      const enoughOfALead = () => {
        if (allMade) return true;                                  // nothing left to wait for
        const perAudio = madeAudio / Math.max(madeChars, 1);        // speech seconds per character
        const perWork = spentWall / Math.max(madeChars, 1);         // seconds of work per character
        let due = ready.reduce((t, piece) => t + piece.buf.duration + piece.gap, 0);
        let lands = 0;
        for (let j = ready.length; j < segments.length; j++) {
          lands += segments[j].length * perWork * SAFETY;
          if (lands > due) return false;
          due += segments[j].length * perAudio + (gaps[j] ?? JOIN_GAP);
        }
        return true;
      };

      const pump = () => {
        if (!ready.length) return;      // every piece failed: there is nothing to say
        if (!started) {
          if (!enoughOfALead()) return;
          started = true;
          nextStart = ctx.currentTime + 0.05;
          meter(analyser);
        }
        for (; queued < ready.length; queued++) {
          const { buf, gap } = ready[queued];
          const src = ctx.createBufferSource();
          src.buffer = buf;
          src.connect(gain);
          // `nextStart` is in the past only if the lead ran out despite everything; starting now
          // is all that is left, and the join is audible. Nothing else can be done about it.
          const when = Math.max(nextStart, ctx.currentTime + 0.02);
          src.start(when);
          sourcesRef.current.push(src);
          last = src;
          nextStart = when + buf.duration + gap;
        }
      };

      for (let i = 0; i < segments.length; i++) {
        // Deafen the wake word for about as long as this one piece should take, and no longer.
        // `pause` on the backend only ever extends, so a generous flat window here is not free:
        // it is why the microphone stayed shut long after Zeta had finished talking. The first
        // piece has nothing measured yet and may have to wait out a parked model coming back,
        // which is 25 s on this machine, so it gets a fixed allowance.
        const perChar = madeChars ? spentWall / madeChars : 0;
        deafen(perChar ? segments[i].length * perChar + 4 : 40);
        const began = performance.now();
        const blob = await api
          .speak(segments[i], { lead: i === 0, final: i === segments.length - 1, signal: ac.signal })
          .catch(() => null);
        if (!blob || !live()) break;
        let buf: AudioBuffer;
        try {
          buf = await ctx.decodeAudioData(await blob.arrayBuffer());
        } catch {
          break;                        // an undecodable piece: speak what there is rather than nothing
        }
        if (!live()) break;
        spentWall += (performance.now() - began) / 1000;
        madeAudio += buf.duration;
        madeChars += segments[i].length;
        ready.push({ buf, gap: i === segments.length - 1 ? 0 : gaps[i] ?? JOIN_GAP });
        pump();
      }
      if (!live()) return;              // a newer reply took over; it owns the state now
      allMade = true;
      pump();                           // a short reply, or a failure part-way, still has to be said

      const endsIn = started ? Math.max(0, nextStart - ctx.currentTime) : 0;
      if (endsIn > 0) {
        deafen(endsIn);                 // now the real length is known, not the estimate
        await new Promise<void>((resolve) => {
          const done = window.setTimeout(resolve, endsIn * 1000 + 200);
          if (last) last.onended = () => { window.clearTimeout(done); resolve(); };
        });
      }
      if (!live()) return;
      window.clearInterval(outTimer.current);
      levels.current.out = 0;
      sourcesRef.current = [];
      abortRef.current = null;
      setSpeaking(false);
    },
    [stopSpeaking],
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
