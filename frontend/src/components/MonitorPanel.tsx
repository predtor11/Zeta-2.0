import { useEffect, useRef, useState } from "react";
import { api } from "../services/api";
import type { GpuMetrics, SystemMetrics } from "../types";

/** Green under half, amber past two thirds, red when it is genuinely a problem. */
function loadColor(percent: number | null | undefined): string {
  const p = percent ?? 0;
  if (p >= 90) return "var(--err)";
  if (p >= 70) return "var(--warn)";
  return "var(--accent)";
}

/** Silicon runs hot by design: 80 C is warm, not broken. */
function tempColor(c: number | null | undefined): string {
  if (c == null) return "var(--muted)";
  if (c >= 90) return "var(--err)";
  if (c >= 80) return "var(--warn)";
  return "var(--accent)";
}

function gb(mb: number | null | undefined): string {
  return mb == null ? "—" : `${(mb / 1024).toFixed(1)} GB`;
}

function uptime(seconds: number): string {
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}

function Meter({ label, percent, value, color }: { label: string; percent: number; value: string; color?: string }) {
  return (
    <div className="mon-meter">
      <div className="mon-meter-head">
        <span className="k">{label}</span>
        <span className="v">{value}</span>
      </div>
      <div className="mon-track">
        <div className="mon-fill" style={{ width: `${Math.min(100, Math.max(0, percent))}%`, background: color || loadColor(percent) }} />
      </div>
    </div>
  );
}

/** One block per logical core. Reads as a texture: a few hot cores look different from all of them. */
function CoreGrid({ cores }: { cores: number[] }) {
  if (!cores.length) return null;
  return (
    <div className="mon-cores" title={`${cores.length} logical cores`}>
      {cores.map((c, i) => (
        <span key={i} className="mon-core" style={{ background: loadColor(c), opacity: 0.25 + (c / 100) * 0.75 }} title={`Core ${i}: ${Math.round(c)}%`} />
      ))}
    </div>
  );
}

/** A rolling history line, so a spike that already passed is still visible. */
function History({ points, color }: { points: number[]; color: string }) {
  if (points.length < 2) return null;
  const w = 100;
  const h = 26;
  const step = w / (points.length - 1);
  const path = points.map((p, i) => `${(i * step).toFixed(2)},${(h - (p / 100) * h).toFixed(2)}`).join(" ");
  return (
    <svg className="mon-spark" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" aria-hidden="true">
      <polyline points={`0,${h} ${path} ${w},${h}`} className="area" style={{ fill: color }} />
      <polyline points={path} className="line" style={{ stroke: color }} />
    </svg>
  );
}

function GpuSection({ gpu }: { gpu: GpuMetrics }) {
  if (!gpu.present) return <p className="mon-none">{gpu.detail || "No NVIDIA GPU detected."}</p>;
  const others = (gpu.processes || []).filter((p) => !/^(python|llama-server|ollama)/i.test(p.name));
  return (
    <>
      <Meter label="Load" percent={gpu.utilization ?? 0} value={`${Math.round(gpu.utilization ?? 0)}%`} />
      <Meter
        label="VRAM"
        percent={gpu.memory_percent ?? 0}
        value={`${gb(gpu.memory_used_mb)} / ${gb(gpu.memory_total_mb)}`}
      />
      <div className="mon-stats">
        <div>
          <span className="k">Temp</span>
          <span className="v" style={{ color: tempColor(gpu.temperature_c) }}>
            {gpu.temperature_c != null ? `${Math.round(gpu.temperature_c)}°C` : "—"}
          </span>
        </div>
        <div>
          <span className="k">Power</span>
          <span className="v">{gpu.power_w != null ? `${Math.round(gpu.power_w)} W` : "—"}</span>
        </div>
        <div>
          <span className="k">Clock</span>
          <span className="v">{gpu.clock_mhz != null ? `${Math.round(gpu.clock_mhz)} MHz` : "—"}</span>
        </div>
        <div>
          <span className="k">Fan</span>
          <span className="v">{gpu.fan_percent != null ? `${Math.round(gpu.fan_percent)}%` : "—"}</span>
        </div>
      </div>
      {others.length > 0 && (
        <p className="mon-note">
          Also using the GPU: {others.map((p) => p.name).join(", ")}. Anything heavy here competes with Zeta for the card.
        </p>
      )}
    </>
  );
}

export default function MonitorPanel() {
  const [m, setM] = useState<SystemMetrics | null>(null);
  const [err, setErr] = useState("");
  const cpuHist = useRef<number[]>([]);
  const gpuHist = useRef<number[]>([]);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const next = await api.metrics();
        if (!alive) return;
        cpuHist.current = [...cpuHist.current, next.cpu.percent].slice(-40);
        gpuHist.current = [...gpuHist.current, next.gpu.utilization ?? 0].slice(-40);
        setM(next);
        setErr("");
      } catch (e) {
        if (alive) setErr((e as Error).message);
      }
    };
    tick();
    const iv = setInterval(tick, 2000);
    return () => {
      alive = false;
      clearInterval(iv);
    };
  }, []);

  if (err && !m) return <div className="panel monitor"><p className="mon-none">Could not read the hardware: {err}</p></div>;
  if (!m) return <div className="panel monitor"><p className="mon-none">Reading the hardware…</p></div>;

  const { cpu, memory, gpu, zeta } = m;
  const llmOnCpu = zeta?.llm?.on_cpu;

  return (
    <div className="panel monitor">
      {/* Zeta's own footprint first: it is the reason anyone opens this panel. */}
      <section className="mon-card zeta">
        <h4>Zeta on this machine</h4>
        <div className="mon-rows">
          <div className={`mon-row ${llmOnCpu ? "bad" : ""}`}>
            <span className="k">Model</span>
            <span className="v">
              {zeta?.llm?.model}
              {zeta?.llm?.loaded === false && " · not loaded"}
              {zeta?.llm?.vram_mb != null && ` · ${gb(zeta.llm.vram_mb)} on the GPU`}
              {llmOnCpu && ` · ${gb(zeta.llm.cpu_mb)} on the CPU`}
            </span>
          </div>
          <div className="mon-row">
            <span className="k">Voice</span>
            <span className="v">
              {zeta?.voice?.provider || "—"}
              {zeta?.voice?.parked ? " · parked in RAM" : zeta?.voice?.device ? ` · on ${zeta.voice.device}` : ""}
            </span>
          </div>
          <div className="mon-row">
            <span className="k">Hearing</span>
            <span className="v">{zeta?.speech?.model || "—"}{zeta?.speech?.device ? ` on ${zeta.speech.device}` : ""}</span>
          </div>
        </div>
        {llmOnCpu && (
          <p className="mon-warn">
            Part of the model does not fit on the GPU and is running on the CPU — roughly ten times slower.
            Close something that is using the card, or lower LLM_CONTEXT_LENGTH.
          </p>
        )}
        {zeta?.sharing && !llmOnCpu && (
          <p className="mon-note">The model and the voice cannot both fit, so Zeta hands the card between them.</p>
        )}
      </section>

      <section className="mon-card">
        <h4>
          CPU <small>{cpu.cores_physical} cores · {cpu.cores_logical} threads{cpu.frequency ? ` · ${(cpu.frequency.current_mhz / 1000).toFixed(1)} GHz` : ""}</small>
        </h4>
        <History points={cpuHist.current} color={loadColor(cpu.percent)} />
        <Meter label="Load" percent={cpu.percent} value={`${Math.round(cpu.percent)}%`} />
        <CoreGrid cores={cpu.per_core} />
        <div className="mon-stats">
          <div>
            <span className="k">Temp</span>
            <span className="v" style={{ color: tempColor(cpu.temperature_c) }}>
              {cpu.temperature_c != null ? `${Math.round(cpu.temperature_c)}°C` : "not available"}
            </span>
          </div>
          {m.battery && (
            <div>
              <span className="k">Battery</span>
              <span className="v">{m.battery.percent}%{m.battery.plugged ? " · charging" : ""}</span>
            </div>
          )}
          <div>
            <span className="k">Uptime</span>
            <span className="v">{uptime(m.uptime_s)}</span>
          </div>
        </div>
        {cpu.temperature_c == null && cpu.temperature_detail && <p className="mon-note">{cpu.temperature_detail}</p>}
      </section>

      <section className="mon-card">
        <h4>GPU <small>{gpu.name || ""}</small></h4>
        {gpu.present && <History points={gpuHist.current} color={loadColor(gpu.utilization ?? 0)} />}
        <GpuSection gpu={gpu} />
      </section>

      <section className="mon-card">
        <h4>Memory</h4>
        <Meter label="RAM" percent={memory.percent} value={`${gb(memory.used_mb)} / ${gb(memory.total_mb)}`} />
        {memory.swap_total_mb > 0 && (
          <Meter
            label="Swap"
            percent={(memory.swap_used_mb / memory.swap_total_mb) * 100}
            value={`${gb(memory.swap_used_mb)} / ${gb(memory.swap_total_mb)}`}
          />
        )}
        {memory.percent >= 90 && (
          <p className="mon-warn">Memory is nearly full — Windows will start paging, which slows everything down.</p>
        )}
      </section>

      <section className="mon-card">
        <h4>Storage</h4>
        {m.disks.map((d) => (
          <Meter key={d.mount} label={d.mount.replace(/\\$/, "")} percent={d.percent} value={`${d.used_gb.toFixed(0)} / ${d.total_gb.toFixed(0)} GB`} />
        ))}
        <div className="mon-stats">
          <div>
            <span className="k">Disk</span>
            <span className="v">{m.disk ? `${m.disk.read_mb_s.toFixed(1)} ↓ ${m.disk.write_mb_s.toFixed(1)} ↑ MB/s` : "—"}</span>
          </div>
          <div>
            <span className="k">Network</span>
            <span className="v">{m.network ? `${m.network.down_mb_s.toFixed(2)} ↓ ${m.network.up_mb_s.toFixed(2)} ↑ MB/s` : "—"}</span>
          </div>
        </div>
      </section>

      {m.processes.length > 0 && (
        <section className="mon-card">
          <h4>Heaviest processes <small>by memory</small></h4>
          <div className="mon-rows">
            {m.processes.map((p) => (
              <div className="mon-row" key={p.pid}>
                <span className="k">{p.name}</span>
                <span className="v">{gb(p.memory_mb)}</span>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
