import { useCallback, useEffect, useRef, useState } from "react";
import { OrderBook } from "./components/OrderBook";
import type { KiwoomWebSocketMessage } from "./components/OrderBook";

// ─────────────────────────────────────────────────────────────────────────────
// Config
// ─────────────────────────────────────────────────────────────────────────────

// In production the SPA is served by Nginx, which proxies /ws → backend.
// In development, Vite's own proxy handles /ws → backend container.
const WS_URL: string =
  import.meta.env.VITE_WS_URL ??
  `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}/ws`;

const TICKER = "044450";
const STOCK_NAME = "KSS해운";

// ─────────────────────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────────────────────

interface TradeTick {
  id: number;
  time: string;
  price: string;
  change: string;
  volume: string;
  volumeValue: number;
  side: "buy" | "sell" | "neutral";
}

// ─────────────────────────────────────────────────────────────────────────────
// Global WebSocket manager (singleton — both App and OrderBook share one socket)
// ─────────────────────────────────────────────────────────────────────────────

function useSharedWebSocket(url: string) {
  const [lastMessage, setLastMessage] = useState<KiwoomWebSocketMessage | null>(null);
  const [connected, setConnected]     = useState(false);
  const [executionStrength, setExecutionStrength] = useState<number | null>(null);
  const wsRef          = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const unmounted      = useRef(false);
  const connectionSeq  = useRef(0);
  const tickIdRef      = useRef(0);
  const recentTickKeys = useRef<string[]>([]);
  const recentTickKeySet = useRef<Set<string>>(new Set());
  const [ticks, setTicks] = useState<TradeTick[]>([]);

  const connect = useCallback(() => {
    if (unmounted.current) return;
    const connectionId = ++connectionSeq.current;
    if (reconnectTimer.current) {
      clearTimeout(reconnectTimer.current);
      reconnectTimer.current = null;
    }
    wsRef.current?.close();

    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      if (connectionSeq.current !== connectionId || unmounted.current) return;
      setConnected(true);
      ws.send(
        JSON.stringify({
          trnm:    "REG",
          grp_no:  "1",
          refresh: "1",
          data: [{ item: [TICKER], type: ["0D", "0B"] }],
        })
      );
    };

    ws.onmessage = (ev: MessageEvent) => {
      if (connectionSeq.current !== connectionId || unmounted.current) return;
      try {
        const msg: KiwoomWebSocketMessage = JSON.parse(ev.data as string);
        setLastMessage(msg);

        // Extract 0B trade ticks for the tape
        if (msg.trnm === "REAL") {
          for (const item of msg.data ?? []) {
            if (item.type === "0B") {
              const v = item.values as Record<string, string>;
              const tickKey = [
                item.item ?? TICKER,
                v["20"] ?? "",
                v["10"] ?? "",
                v["15"] ?? "",
                v["13"] ?? "",
              ].join("|");
              if (recentTickKeySet.current.has(tickKey)) {
                continue;
              }
              recentTickKeySet.current.add(tickKey);
              recentTickKeys.current.push(tickKey);
              while (recentTickKeys.current.length > 300) {
                const oldKey = recentTickKeys.current.shift();
                if (oldKey) recentTickKeySet.current.delete(oldKey);
              }

              const strength = parseFloat((v["228"] ?? "").replace(/[, ]/g, ""));
              if (!Number.isNaN(strength)) {
                setExecutionStrength(strength);
              }
              const side =
                (v["15"] ?? "").startsWith("+")
                  ? "buy"
                  : (v["15"] ?? "").startsWith("-")
                  ? "sell"
                  : "neutral";
              const tick: TradeTick = {
                id:     ++tickIdRef.current,
                time:   fmtTime(v["20"] ?? ""),
                price:  fmtNum(v["10"] ?? ""),
                change: v["11"] ?? "",
                volume: fmtNum(Math.abs(parseSignedQty(v["15"] ?? "")).toString()),
                volumeValue: Math.abs(parseSignedQty(v["15"] ?? "")),
                side,
              };
              setTicks((prev) => [tick, ...prev].slice(0, 120));
            }
          }
        }
      } catch {
        /* ignore malformed frames */
      }
    };

    ws.onclose = () => {
      if (connectionSeq.current !== connectionId) return;
      setConnected(false);
      if (!unmounted.current) {
        reconnectTimer.current = setTimeout(connect, 2_000);
      }
    };

    ws.onerror = () => {
      if (connectionSeq.current !== connectionId) return;
      ws.close();
    };
  }, [url]);

  useEffect(() => {
    unmounted.current = false;
    connect();
    return () => {
      unmounted.current = true;
      connectionSeq.current += 1;
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      reconnectTimer.current = null;
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [connect]);

  return { lastMessage, connected, ticks, executionStrength };
}

// ─────────────────────────────────────────────────────────────────────────────
// Formatters
// ─────────────────────────────────────────────────────────────────────────────

function fmtNum(s: string): string {
  const n = parseInt(s.replace(/[+, ]/g, ""), 10);
  return isNaN(n) ? s : n.toLocaleString("ko-KR");
}

function parseSignedQty(s: string): number {
  const n = parseInt(s.replace(/[, ]/g, ""), 10);
  return isNaN(n) ? 0 : n;
}

function fmtTime(raw: string): string {
  if (raw.length < 6) return "--:--:--";
  return `${raw.slice(0, 2)}:${raw.slice(2, 4)}:${raw.slice(4, 6)}`;
}

// ─────────────────────────────────────────────────────────────────────────────
// Sub-components
// ─────────────────────────────────────────────────────────────────────────────

function StatusDot({ live }: { live: boolean }) {
  return (
    <span
      className={[
        "inline-block w-2 h-2 rounded-full",
        live ? "bg-green-400 animate-pulse" : "bg-content-muted",
      ].join(" ")}
    />
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// App
// ─────────────────────────────────────────────────────────────────────────────

export default function App() {
  const { lastMessage, connected, ticks, executionStrength } = useSharedWebSocket(WS_URL);
  const [panelOpacity, setPanelOpacity] = useState(0.82);

  return (
    <div className="min-h-dvh bg-surface font-sans text-content-primary">
      {/* ── Top bar ──────────────────────────────────────────────────────── */}
      <header className="sticky top-0 z-10 flex items-center justify-between px-4 py-3 bg-surface-card border-b border-surface-border">
        <div className="flex items-center gap-3">
          <span className="text-sm font-semibold text-content-secondary tracking-wide uppercase">
            phinguin dashboard
          </span>
          <span className="text-xs text-content-muted">{TICKER}</span>
          <div className="flex items-center gap-1.5 text-[10px] text-content-muted/70">
            <span>{Math.round(panelOpacity * 100)}</span>
            <input
              className="h-1 w-20 accent-slate-400 opacity-60"
              type="range"
              min="35"
              max="100"
              value={Math.round(panelOpacity * 100)}
              aria-label="panel opacity"
              onChange={(event) => setPanelOpacity(Number(event.currentTarget.value) / 100)}
            />
          </div>
        </div>
        <div className="flex items-center gap-2 text-xs text-content-secondary">
          <StatusDot live={connected} />
          {connected ? "실시간 연결됨" : "연결 중…"}
        </div>
      </header>

      {/* ── Main grid ────────────────────────────────────────────────────── */}
      <main className="mx-auto max-w-[460px] px-4 py-6">
        <section
          className="flex items-start justify-center"
          aria-label="stealth market panel"
          style={{ opacity: panelOpacity }}
        >
          <OrderBook
            ticker={TICKER}
            stockName={STOCK_NAME}
            rawMessage={lastMessage}
            tapeTicks={ticks}
            executionStrength={executionStrength}
          />
        </section>
      </main>
    </div>
  );
}
