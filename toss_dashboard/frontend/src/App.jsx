import { useCallback, useEffect, useRef, useState } from "react";
import Panel from "./components/Panel";
import Table from "./components/Table";
import MacroGrid from "./components/MacroGrid";
import EventLog from "./components/EventLog";
import { money, num, pct, signClass } from "./format";

const API_BASE = import.meta.env.VITE_API_BASE ?? "";

// Dashboard token: open the page once with ?token=..., it is remembered after that.
function readToken() {
  const fromUrl = new URLSearchParams(location.search).get("token");
  try {
    if (fromUrl) localStorage.setItem("trader_token", fromUrl);
    return fromUrl ?? localStorage.getItem("trader_token") ?? "";
  } catch {
    return fromUrl ?? "";
  }
}
const TOKEN = readToken();
const WS_BASE = API_BASE
  ? API_BASE.replace(/^http/, "ws")
  : `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}`;
const WS_URL = `${WS_BASE}/ws?token=${encodeURIComponent(TOKEN)}`;

function useEngineState() {
  const [state, setState] = useState(null);
  const [connected, setConnected] = useState(false);
  const retry = useRef(0);

  useEffect(() => {
    let socket;
    let closed = false;
    const connect = () => {
      socket = new WebSocket(WS_URL);
      socket.onopen = () => {
        retry.current = 0;
        setConnected(true);
      };
      socket.onmessage = (e) => setState(JSON.parse(e.data));
      socket.onclose = () => {
        setConnected(false);
        if (!closed) setTimeout(connect, Math.min(10000, 500 * 2 ** retry.current++));
      };
    };
    connect();
    return () => {
      closed = true;
      socket?.close();
    };
  }, []);
  return { state, connected };
}

const positionCols = [
  { key: "symbol", label: "Symbol" },
  { key: "qty", label: "Qty", fmt: (v) => num(v, 0) },
  { key: "entry", label: "Entry", fmt: (v) => num(v) },
  { key: "last", label: "Last", fmt: (v) => num(v) },
  { key: "stop", label: "Stop", fmt: (v) => num(v) },
  { key: "pnl", label: "P&L", fmt: money, cls: signClass },
  { key: "ret", label: "Return", fmt: pct, cls: signClass },
  { key: "overnight", label: "Overnight", fmt: (v) => (v ? "yes" : "") },
];

const setupCols = [
  { key: "symbol", label: "Symbol" },
  { key: "rvol", label: "RVOL", fmt: (v) => `${num(v, 1)}x` },
  { key: "state", label: "State" },
  { key: "trigger", label: "Trigger", fmt: (v) => (v ? num(v) : "—") },
  { key: "last", label: "Last", fmt: (v) => num(v) },
  { key: "atr", label: "ATR14", fmt: (v) => num(v) },
  { key: "note", label: "Note" },
];

const tradeCols = [
  { key: "closed", label: "Closed", fmt: (v) => (v ? v.slice(5, 16).replace("T", " ") : "") },
  { key: "symbol", label: "Symbol" },
  { key: "qty", label: "Qty", fmt: (v) => num(v, 0) },
  { key: "entry", label: "Entry", fmt: (v) => num(v) },
  { key: "exit", label: "Exit", fmt: (v) => num(v) },
  { key: "pnl", label: "P&L", fmt: money, cls: signClass },
  { key: "r_multiple", label: "R", fmt: (v) => (v == null ? "—" : num(v, 2)), cls: signClass },
  { key: "reason", label: "Exit reason" },
];

function Stat({ label, value, cls = "" }) {
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-950 px-4 py-3">
      <div className="text-[11px] uppercase tracking-widest text-zinc-500">{label}</div>
      <div className={`mt-1 text-xl tabular-nums ${cls}`}>{value}</div>
    </div>
  );
}

function Button({ children, onClick, tone = "zinc", disabled }) {
  const tones = {
    zinc: "border-zinc-700 bg-zinc-900 text-zinc-200",
    green: "border-emerald-700 bg-emerald-950 text-emerald-300",
    amber: "border-amber-700 bg-amber-950 text-amber-300",
    red: "border-red-800 bg-red-950 text-red-300",
  };
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={`rounded border px-3 py-1.5 text-xs disabled:opacity-40 ${tones[tone]}`}
    >
      {children}
    </button>
  );
}

export default function App() {
  const { state, connected } = useEngineState();
  const [busy, setBusy] = useState(false);

  const call = useCallback(async (path, confirmText) => {
    if (confirmText && !window.confirm(confirmText)) return;
    setBusy(true);
    try {
      await fetch(`${API_BASE}${path}`, { method: "POST", headers: { "X-Token": TOKEN } });
    } finally {
      setBusy(false);
    }
  }, []);

  const s = state ?? {};
  const live = s.mode === "live";
  const session = s.session?.open
    ? `${s.session.open.slice(11, 16)}–${s.session.close.slice(11, 16)} ET`
    : "market closed";

  return (
    <div className="min-h-dvh px-4 py-5 font-mono sm:px-6">
      <header className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <h1 className="text-lg tracking-tight text-zinc-100">Toss Auto Trader</h1>
          <span
            className={`rounded px-2 py-0.5 text-[11px] font-semibold ${
              live ? "bg-red-600 text-white" : "bg-sky-900 text-sky-200"
            }`}
          >
            {live ? "LIVE" : "PAPER"}
          </span>
          {s.paused && <span className="rounded bg-amber-900 px-2 py-0.5 text-[11px] text-amber-200">PAUSED</span>}
          {s.risk?.halted && (
            <span className="rounded bg-red-900 px-2 py-0.5 text-[11px] text-red-200">HALTED: {s.risk.reason}</span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-3 text-xs text-zinc-400">
          <span className="flex items-center gap-1.5">
            <span className={`h-2 w-2 rounded-full ${connected ? "bg-emerald-400" : "bg-zinc-600"}`} />
            dashboard
          </span>
          <span className="flex items-center gap-1.5">
            <span className={`h-2 w-2 rounded-full ${s.connected ? "bg-emerald-400" : "bg-zinc-600"}`} />
            toss stream
          </span>
          <span>{s.now_et ?? "—"} ET</span>
          <span className="text-zinc-600">|</span>
          <span>{session}</span>
        </div>
      </header>

      {s.warnings?.length > 0 && (
        <div className="mb-4 rounded border border-amber-800 bg-amber-950/40 px-3 py-2 text-xs text-amber-200">
          {s.warnings.map((w) => (
            <div key={w}>⚠ {w}</div>
          ))}
        </div>
      )}

      <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        <Stat label="Equity" value={money(s.equity)} />
        <Stat label="Day P&L" value={money(s.day_pnl)} cls={signClass(s.day_pnl)} />
        <Stat label="Realized" value={money(s.realized_pnl)} cls={signClass(s.realized_pnl)} />
        <Stat label="Unrealized" value={money(s.unrealized_pnl)} cls={signClass(s.unrealized_pnl)} />
        <Stat label="Positions" value={`${s.positions?.length ?? 0}`} />
        <Stat label="Orders today" value={`${s.risk?.orders_today ?? 0}`} />
      </div>

      <div className="mb-4 flex flex-wrap gap-2">
        <Button tone="green" disabled={busy || s.running} onClick={() => call("/api/start")}>
          Start engine
        </Button>
        <Button disabled={busy || !s.running} onClick={() => call("/api/stop")}>
          Stop engine
        </Button>
        {s.paused ? (
          <Button tone="green" disabled={busy} onClick={() => call("/api/resume")}>
            Resume entries
          </Button>
        ) : (
          <Button tone="amber" disabled={busy || !s.running} onClick={() => call("/api/pause")}>
            Pause new entries
          </Button>
        )}
        <Button
          tone="red"
          disabled={busy || !s.running || !s.positions?.length}
          onClick={() => call("/api/flatten", "Sell every position the bot opened?")}
        >
          Flatten bot positions
        </Button>
      </div>

      <div className="mb-4">
        <MacroGrid macro={s.macro ?? {}} />
      </div>

      <div className="mb-4 grid gap-4 xl:grid-cols-2">
        <Panel title="Positions" right={`${s.positions?.length ?? 0} open`}>
          <Table columns={positionCols} rows={s.positions ?? []} empty="No open positions" />
        </Panel>
        <Panel title="Stocks in play" right={`${s.setups?.length ?? 0} candidates`}>
          <Table columns={setupCols} rows={s.setups ?? []} empty="Scan runs at 09:35:10 ET" />
        </Panel>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Panel title="Closed trades" right={`${s.trades?.length ?? 0}`}>
          <Table columns={tradeCols} rows={[...(s.trades ?? [])].reverse()} empty="No trades yet" />
        </Panel>
        <Panel title="Engine log">
          <EventLog events={s.events ?? []} />
        </Panel>
      </div>
    </div>
  );
}
