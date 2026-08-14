import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { io } from "socket.io-client";

const SOCKET_URL = import.meta.env.VITE_SOCKET_URL ?? "";
const API_BASE = import.meta.env.VITE_API_BASE ?? "";
const MAX_LOGS = 400;
const VPIN_ALERT = 0.7;

function fmt(n, digits = 2) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
  return Number(n).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function LiveTerminal({ lines }) {
  const scroller = useRef(null);

  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [lines]);

  return (
    <section className="rounded-lg border border-zinc-800 bg-black overflow-hidden">
      <header className="flex items-center justify-between border-b border-zinc-800 px-3 py-2 text-[11px] uppercase tracking-widest text-zinc-500">
        <span>live console</span>
        <span>{lines.length} lines</span>
      </header>
      <div
        ref={scroller}
        className="h-[420px] overflow-y-auto px-3 py-2 font-mono text-[12px] leading-5 text-emerald-300"
      >
        {lines.length === 0 ? (
          <div className="text-zinc-600">waiting for trading_logs…</div>
        ) : (
          lines.map((line) => (
            <div
              key={line.id}
              className={
                line.level === "WARNING" || line.level === "ERROR" || line.level === "CRITICAL"
                  ? "text-amber-300"
                  : "text-emerald-300"
              }
            >
              {line.ts || line.message}
            </div>
          ))
        )}
      </div>
    </section>
  );
}

function VolumeClockBar({ totalVol, targetVol }) {
  const pct = targetVol > 0 ? Math.min(100, (totalVol / targetVol) * 100) : 0;
  return (
    <section className="rounded-lg border border-zinc-800 bg-zinc-950 p-4">
      <div className="mb-2 flex items-end justify-between font-mono text-xs text-zinc-400">
        <span className="uppercase tracking-widest text-zinc-500">volume clock</span>
        <span>
          {fmt(totalVol, 0)} / {fmt(targetVol, 0)} ({pct.toFixed(1)}%)
        </span>
      </div>
      <div className="h-3 overflow-hidden rounded bg-zinc-800">
        <div
          className="h-full bg-cyan-400 transition-all duration-300"
          style={{ width: `${pct}%` }}
        />
      </div>
    </section>
  );
}

function MetricCard({ label, value, alert }) {
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-950 p-4">
      <div className="text-[11px] uppercase tracking-widest text-zinc-500">{label}</div>
      <div className={`mt-2 font-mono text-2xl ${alert ? "text-red-500" : "text-zinc-100"}`}>
        {value}
      </div>
    </div>
  );
}

export default function App() {
  const [connected, setConnected] = useState(false);
  const [running, setRunning] = useState(false);
  const [busy, setBusy] = useState(false);
  const [logs, setLogs] = useState([]);
  const [state, setState] = useState({
    regime: "—",
    vpin: null,
    price: null,
    ofi: null,
    ticks: 0,
    total_vol: 0,
    target_vol: 0,
  });
  const logId = useRef(0);

  const refreshStatus = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/status`);
      const data = await res.json();
      setRunning(Boolean(data.running));
      if (data.state) setState((prev) => ({ ...prev, ...data.state }));
    } catch {
      /* backend may not be up yet */
    }
  }, []);

  useEffect(() => {
    refreshStatus();
  }, [refreshStatus]);

  useEffect(() => {
    const socket = io(SOCKET_URL || undefined, {
      transports: ["websocket", "polling"],
    });

    const onConnect = () => setConnected(true);
    const onDisconnect = () => setConnected(false);
    const onLog = (payload) => {
      const id = ++logId.current;
      setLogs((prev) => [...prev, { id, ...payload }].slice(-MAX_LOGS));
    };
    const onState = (payload) => {
      if (!payload || Object.keys(payload).length === 0) return;
      setState((prev) => ({ ...prev, ...payload }));
    };

    socket.on("connect", onConnect);
    socket.on("disconnect", onDisconnect);
    socket.on("trading_logs", onLog);
    socket.on("system_state", onState);

    return () => {
      socket.off("connect", onConnect);
      socket.off("disconnect", onDisconnect);
      socket.off("trading_logs", onLog);
      socket.off("system_state", onState);
      socket.disconnect();
    };
  }, []);

  const control = useCallback(
    async (path) => {
      setBusy(true);
      try {
        const res = await fetch(`${API_BASE}${path}`, { method: "POST" });
        const data = await res.json();
        setRunning(Boolean(data.running));
        await refreshStatus();
      } finally {
        setBusy(false);
      }
    },
    [refreshStatus]
  );

  const vpinAlert = useMemo(
    () => state.vpin !== null && state.vpin !== undefined && Number(state.vpin) > VPIN_ALERT,
    [state.vpin]
  );

  return (
    <div className="min-h-dvh bg-[#05070a] px-6 py-6 font-mono text-zinc-200">
      <header className="mb-6 flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-lg tracking-tight text-zinc-100">Toss 24h Microstructure Desk</h1>
          <p className="text-xs text-zinc-500">Flask-SocketIO · Quantitative24HourBot</p>
        </div>
        <div className="flex items-center gap-3 text-xs">
          <span className={`h-2 w-2 rounded-full ${connected ? "bg-emerald-400" : "bg-zinc-600"}`} />
          <span>{connected ? "socket live" : "socket down"}</span>
          <span className="text-zinc-600">|</span>
          <span>{running ? "bot running" : "bot stopped"}</span>
        </div>
      </header>

      <div className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <MetricCard label="Regime" value={state.regime || "—"} />
        <MetricCard label="VPIN Toxicity" value={fmt(state.vpin, 3)} alert={vpinAlert} />
        <MetricCard label="Price" value={fmt(state.price, 2)} />
        <MetricCard label="OFI" value={fmt(state.ofi, 1)} />
      </div>

      <div className="mb-4">
        <VolumeClockBar
          totalVol={Number(state.total_vol) || 0}
          targetVol={Number(state.target_vol) || 0}
        />
        <p className="mt-2 text-[11px] text-zinc-500">ticks {fmt(state.ticks, 0)}</p>
      </div>

      <div className="mb-4 flex gap-2">
        <button
          type="button"
          disabled={busy || running}
          onClick={() => control("/api/start")}
          className="rounded border border-emerald-700 bg-emerald-950 px-4 py-2 text-sm text-emerald-300 disabled:opacity-40"
        >
          Start
        </button>
        <button
          type="button"
          disabled={busy || !running}
          onClick={() => control("/api/stop")}
          className="rounded border border-red-800 bg-red-950 px-4 py-2 text-sm text-red-300 disabled:opacity-40"
        >
          Stop
        </button>
      </div>

      <LiveTerminal lines={logs} />
    </div>
  );
}
