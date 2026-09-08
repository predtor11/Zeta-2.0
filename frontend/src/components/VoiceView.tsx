import { useEffect, useRef } from "react";
import type { VoiceState } from "../hooks/useVoice";
import { moodRGB } from "../lib/mood";
import type { ChatMessage, EmotionReading, SystemStatus } from "../types";

interface Props {
  voice: VoiceState;
  busy: boolean;
  status: SystemStatus | null;
  lastUser?: ChatMessage;
  emotion?: EmotionReading | null;
  onStop: () => void;
}

const COLORS: Record<string, [number, number, number]> = {
  idle: [0, 212, 255],
  listening: [46, 230, 166],
  transcribing: [46, 230, 166],
  thinking: [255, 181, 71],
  speaking: [124, 92, 255],
  off: [125, 140, 163],
};


/** The stage: one orb that is also the button. Click = start/stop listening. */
export default function VoiceView({ voice, busy, status, lastUser, emotion, onStop }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const phaseRef = useRef(voice.phase);
  phaseRef.current = voice.phase;
  const hoverRef = useRef(false);
  // How the person seems, blended into the orb so the light in the room changes with the mood.
  const moodRef = useRef<{ rgb: number[]; weight: number }>({ rgb: [0, 0, 0], weight: 0 });
  moodRef.current =
    emotion && emotion.confidence >= 0.3
      ? { rgb: moodRGB(emotion.valence, emotion.arousal), weight: Math.min(0.5, 0.25 + emotion.confidence * 0.35) }
      : { rgb: [0, 0, 0], weight: 0 };

  useEffect(() => {
    const canvas = canvasRef.current!;
    const ctx = canvas.getContext("2d")!;
    let raf = 0;
    let t = 0;
    let level = 0;
    let hover = 0;
    const color = COLORS.idle.slice() as number[];
    const particles = Array.from({ length: 64 }, (_, i) => ({ a: (i / 64) * Math.PI * 2, r: 1.3 + Math.random() * 0.9, s: 0.1 + Math.random() * 0.5, o: Math.random() }));

    const resize = () => {
      const dpr = window.devicePixelRatio || 1;
      const { clientWidth: w, clientHeight: h } = canvas;
      canvas.width = w * dpr;
      canvas.height = h * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(canvas);

    const draw = () => {
      const w = canvas.clientWidth;
      const h = canvas.clientHeight;
      const cx = w / 2;
      const cy = h / 2;
      const R = Math.min(w, h) * 0.17;
      const phase = phaseRef.current;
      const target = phase === "listening" ? voice.levels.current.mic : phase === "speaking" ? voice.levels.current.out : 0;
      level += (target - level) * 0.25;
      hover += ((hoverRef.current ? 1 : 0) - hover) * 0.1;
      const base = COLORS[phase] || COLORS.idle;
      const { rgb: mood, weight } = moodRef.current;
      const want = weight > 0 ? base.map((c, i) => c * (1 - weight) + mood[i] * weight) : base;
      for (let i = 0; i < 3; i++) color[i] += (want[i] - color[i]) * 0.06;
      const [r, g, b] = color.map(Math.round);
      const rgba = (a: number) => `rgba(${r},${g},${b},${a})`;
      const speed = phase === "thinking" ? 0.05 : phase === "idle" ? 0.012 : 0.028;
      t += speed;

      ctx.clearRect(0, 0, w, h);
      const halo = ctx.createRadialGradient(cx, cy, R * 0.4, cx, cy, R * (2.6 + level * 0.9 + hover * 0.2));
      halo.addColorStop(0, rgba(0.26 + level * 0.25 + hover * 0.06));
      halo.addColorStop(0.5, rgba(0.06));
      halo.addColorStop(1, rgba(0));
      ctx.fillStyle = halo;
      ctx.fillRect(0, 0, w, h);

      for (let ring = 0; ring < 3; ring++) {
        const base = R * (1 + ring * 0.24 + hover * 0.03) + level * R * (0.35 + ring * 0.12);
        ctx.beginPath();
        for (let i = 0; i <= 140; i++) {
          const a = (i / 140) * Math.PI * 2;
          const wob = Math.sin(a * (3 + ring) + t * (1.6 + ring * 0.7)) * (R * 0.035 + level * R * 0.12) + Math.sin(a * 7 - t * 2.3) * R * 0.015;
          const rr = base + wob;
          const x = cx + Math.cos(a) * rr;
          const y = cy + Math.sin(a) * rr;
          i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
        }
        ctx.closePath();
        ctx.strokeStyle = rgba(0.55 - ring * 0.15);
        ctx.lineWidth = 2 - ring * 0.5;
        ctx.stroke();
      }

      const breathe = 1 + Math.sin(t * 1.4) * 0.03 + level * 0.25 + hover * 0.04;
      const core = ctx.createRadialGradient(cx - R * 0.25, cy - R * 0.3, R * 0.1, cx, cy, R * breathe);
      core.addColorStop(0, `rgba(255,255,255,${0.55 + level * 0.3})`);
      core.addColorStop(0.35, rgba(0.75));
      core.addColorStop(1, rgba(0.12));
      ctx.fillStyle = core;
      ctx.beginPath();
      ctx.arc(cx, cy, R * breathe, 0, Math.PI * 2);
      ctx.fill();

      if (phase === "thinking" || phase === "transcribing") {
        for (let k = 0; k < 3; k++) {
          ctx.beginPath();
          ctx.arc(cx, cy, R * (1.6 + k * 0.2), t * (1.5 + k * 0.4) + k, t * (1.5 + k * 0.4) + k + Math.PI * (0.5 + k * 0.2));
          ctx.strokeStyle = rgba(0.6 - k * 0.15);
          ctx.lineWidth = 2;
          ctx.stroke();
        }
      }

      for (const p of particles) {
        p.a += p.s * 0.01 * (phase === "idle" ? 0.5 : 1 + level * 2);
        const rr = R * (p.r + Math.sin(t * 2 + p.a * 3) * 0.05) + level * R * 0.5 * p.o;
        const x = cx + Math.cos(p.a) * rr;
        const y = cy + Math.sin(p.a) * rr;
        ctx.fillStyle = rgba(0.12 + p.o * 0.5);
        ctx.beginPath();
        ctx.arc(x, y, 1 + p.o * 1.6 + level * 1.5, 0, Math.PI * 2);
        ctx.fill();
      }
      raf = requestAnimationFrame(draw);
    };
    raf = requestAnimationFrame(draw);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const wake = status?.voice?.wake;
  const hint =
    voice.phase === "listening" ? "Listening… I'll send when you pause (click or Space to send now)"
    : voice.phase === "transcribing" ? "Understanding…"
    : voice.phase === "thinking" ? "Working on it…"
    : voice.phase === "speaking" ? "Speaking… click the orb to interrupt"
    : voice.phase === "off" ? "Voice is off. Enable speech in Settings."
    : voice.enabled ? `Click the orb or press Space${wake?.running ? `, or say “${wake.phrase}”` : ""}`
    : "Voice input is off. Enable speech-to-text in Settings.";

  return (
    <div className={`stage phase-${voice.phase}`}>
      <div
        className="orb-wrap"
        onClick={() => (voice.enabled ? voice.toggle() : undefined)}
        onMouseEnter={() => (hoverRef.current = true)}
        onMouseLeave={() => (hoverRef.current = false)}
        title={voice.enabled ? "Click to talk (Space)" : ""}
      >
        <canvas ref={canvasRef} className="orb" />
        <div className="orb-label"><span className="phase">{voice.phase === "off" ? "OFFLINE" : voice.phase === "idle" ? "ZETA" : voice.phase.toUpperCase()}</span></div>
      </div>
      <div className="stage-bottom">
        <div className="voice-caption">
          {lastUser && <div className="you fade-up" key={lastUser.id}>“{lastUser.content}”</div>}
          {voice.error && <div className="err fade-up">{voice.error}</div>}
        </div>
        <div className="voice-hint">
          {hint}
          {busy && <button className="btn sm ghost stop-pill" onClick={onStop}>■ stop</button>}
        </div>
      </div>
    </div>
  );
}
