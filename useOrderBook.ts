import { useState, useEffect, useRef, useCallback } from "react";
import {
  OrderBookRow,
  OrderBookData,
  KiwoomHogaValues,
  KiwoomWebSocketMessage,
  UseOrderBookConfig,
} from "./orderBook.types";

// ─────────────────────────────────────────────────────────────────────────────
// Constants
// ─────────────────────────────────────────────────────────────────────────────

const FLASH_DURATION_MS = 280;
const RECONNECT_DELAY_MS = 2_000;
const MAX_RECONNECT_ATTEMPTS = 10;

// ─────────────────────────────────────────────────────────────────────────────
// Pure parsing helpers
// ─────────────────────────────────────────────────────────────────────────────

/** Strip leading +/- and commas; return 0 on empty/invalid. */
function parseIntField(raw: string | undefined): number {
  if (!raw) return 0;
  const n = parseInt(raw.replace(/[+,]/g, "").trim(), 10);
  return isNaN(n) ? 0 : Math.abs(n);
}

/**
 * Map Kiwoom 0D `values` object into structured arrays.
 *
 * Ask prices:   FID 41–50  (rank 1 = FID 41, rank 10 = FID 50)
 * Ask volumes:  FID 61–70
 * Bid prices:   FID 51–60  (rank 1 = FID 51, rank 10 = FID 60)
 * Bid volumes:  FID 71–80
 * Total ask:    FID 121
 * Total bid:    FID 125
 * Hora time:    FID 21
 */
function parseHogaValues(
  values: KiwoomHogaValues,
  prevVolumes: React.MutableRefObject<Map<string, number>>,
  flashingKeys: Set<string>
): Pick<OrderBookData, "asks" | "bids" | "totalAskVolume" | "totalBidVolume" | "horaTime"> {
  const asks: OrderBookRow[] = [];
  const bids: OrderBookRow[] = [];

  for (let i = 1; i <= 10; i++) {
    const askPrice = parseIntField(values[String(40 + i)]);
    const askVol = parseIntField(values[String(60 + i)]);
    const bidPrice = parseIntField(values[String(50 + i)]);
    const bidVol = parseIntField(values[String(70 + i)]);

    const askKey = `ask-${i}`;
    const bidKey = `bid-${i}`;

    const prevAsk = prevVolumes.current.get(askKey);
    if (prevAsk !== undefined && prevAsk !== askVol) flashingKeys.add(askKey);
    prevVolumes.current.set(askKey, askVol);

    const prevBid = prevVolumes.current.get(bidKey);
    if (prevBid !== undefined && prevBid !== bidVol) flashingKeys.add(bidKey);
    prevVolumes.current.set(bidKey, bidVol);

    asks.push({
      price: askPrice,
      volume: askVol,
      type: "ask",
      rank: i,
      isFlashing: flashingKeys.has(askKey),
    });
    bids.push({
      price: bidPrice,
      volume: bidVol,
      type: "bid",
      rank: i,
      isFlashing: flashingKeys.has(bidKey),
    });
  }

  return {
    asks,
    bids,
    totalAskVolume: parseIntField(values["121"]),
    totalBidVolume: parseIntField(values["125"]),
    horaTime: values["21"] ?? "",
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// Initial state
// ─────────────────────────────────────────────────────────────────────────────

function buildEmptyState(ticker: string): OrderBookData & { isConnected: boolean } {
  const emptyRows = (side: "ask" | "bid"): OrderBookRow[] =>
    Array.from({ length: 10 }, (_, i) => ({
      price: 0,
      volume: 0,
      type: side,
      rank: i + 1,
      isFlashing: false,
    }));

  return {
    asks: emptyRows("ask"),
    bids: emptyRows("bid"),
    totalAskVolume: 0,
    totalBidVolume: 0,
    horaTime: "",
    ticker,
    isConnected: false,
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// Hook
// ─────────────────────────────────────────────────────────────────────────────

export interface OrderBookState extends OrderBookData {
  isConnected: boolean;
}

export function useOrderBook(config: UseOrderBookConfig): OrderBookState {
  const { wsUrl, ticker, rawMessage, onConnect, onDisconnect } = config;

  const [state, setState] = useState<OrderBookState>(() => buildEmptyState(ticker));

  // Ref-based bookkeeping that must not cause re-renders on mutation.
  const prevVolumes = useRef<Map<string, number>>(new Map());
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectCount = useRef(0);
  const flashTimeout = useRef<ReturnType<typeof setTimeout> | null>(null);
  const unmounted = useRef(false);

  // ── Message processor (shared between both WS-managed and rawMessage modes) ──
  const processMessage = useCallback(
    (msg: KiwoomWebSocketMessage) => {
      if (msg.trnm !== "REAL") return;

      const hogaItem = msg.data?.find((d) => d.type === "0D");
      if (!hogaItem?.values) return;

      // Collect which keys changed so we can set isFlashing = true
      const flashingKeys = new Set<string>();
      const parsed = parseHogaValues(hogaItem.values, prevVolumes, flashingKeys);

      setState((prev) => ({
        ...prev,
        ...parsed,
        ticker: hogaItem.item || ticker,
        isConnected: true,
      }));

      // Clear any previous flash timer, schedule a new one to reset isFlashing.
      if (flashingKeys.size > 0) {
        if (flashTimeout.current) clearTimeout(flashTimeout.current);
        flashTimeout.current = setTimeout(() => {
          if (unmounted.current) return;
          setState((prev) => ({
            ...prev,
            asks: prev.asks.map((r) => ({ ...r, isFlashing: false })),
            bids: prev.bids.map((r) => ({ ...r, isFlashing: false })),
          }));
        }, FLASH_DURATION_MS);
      }
    },
    [ticker]
  );

  // ── Mode A: rawMessage prop — caller owns the WebSocket ──────────────────
  useEffect(() => {
    if (!rawMessage) return;
    processMessage(rawMessage);
  }, [rawMessage, processMessage]);

  // ── Mode B: wsUrl — hook manages its own connection ───────────────────────
  useEffect(() => {
    if (!wsUrl || rawMessage !== undefined) return; // let Mode A take over if rawMessage is provided

    unmounted.current = false;

    function connect() {
      if (unmounted.current) return;
      if (reconnectCount.current >= MAX_RECONNECT_ATTEMPTS) {
        console.warn("[useOrderBook] Max reconnect attempts reached.");
        return;
      }

      const ws = new WebSocket(wsUrl!);
      wsRef.current = ws;

      ws.onopen = () => {
        reconnectCount.current = 0;
        setState((prev) => ({ ...prev, isConnected: true }));
        onConnect?.();

        // Register for 0D real-time data on this ticker.
        ws.send(
          JSON.stringify({
            trnm: "REG",
            grp_no: "2",
            refresh: "1",
            data: [{ item: [ticker], type: ["0D"] }],
          })
        );
      };

      ws.onmessage = (event: MessageEvent) => {
        try {
          const msg: KiwoomWebSocketMessage = JSON.parse(event.data as string);
          processMessage(msg);
        } catch (err) {
          console.error("[useOrderBook] JSON parse error:", err);
        }
      };

      ws.onclose = () => {
        if (unmounted.current) return;
        setState((prev) => ({ ...prev, isConnected: false }));
        onDisconnect?.();
        reconnectCount.current += 1;
        setTimeout(connect, RECONNECT_DELAY_MS);
      };

      ws.onerror = (err) => {
        console.error("[useOrderBook] WebSocket error:", err);
        ws.close(); // triggers onclose → reconnect
      };
    }

    connect();

    return () => {
      unmounted.current = true;
      if (flashTimeout.current) clearTimeout(flashTimeout.current);
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [wsUrl, ticker, rawMessage, processMessage, onConnect, onDisconnect]);

  return state;
}
