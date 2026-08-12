// ─────────────────────────────────────────────────────────────────────────────
// Kiwoom 주식호가잔량 (TR: 0D) — strict TypeScript interfaces
// ─────────────────────────────────────────────────────────────────────────────

export type OrderSide = "ask" | "bid";

/** A single price level in the order book. rank 1 = closest to the spread. */
export interface OrderBookRow {
  price: number;
  volume: number;
  type: OrderSide;
  rank: number; // 1–10
  /** True for one render cycle when volume changes (triggers flash animation). */
  isFlashing: boolean;
}

/** Parsed, clean order book snapshot ready for the UI. */
export interface OrderBookData {
  asks: OrderBookRow[]; // 매도 — index 0 = rank 1 (closest to spread)
  bids: OrderBookRow[]; // 매수 — index 0 = rank 1 (closest to spread)
  totalAskVolume: number; // 매도호가총잔량 — FID 121
  totalBidVolume: number; // 매수호가총잔량 — FID 125
  horaTime: string; // 호가시간 — FID 21 (HHMMSS raw string)
  ticker: string;
}

// ─────────────────────────────────────────────────────────────────────────────
// Raw Kiwoom WebSocket payload shapes (as documented by Kiwoom Open API)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * The `values` map uses numeric-string keys.
 * Key ranges for 주식호가잔량 (0D):
 *   41–50  매도호가 1~10
 *   61–70  매도호가수량 1~10
 *   51–60  매수호가 1~10
 *   71–80  매수호가수량 1~10
 *   121    매도호가총잔량
 *   125    매수호가총잔량
 *   21     호가시간
 */
export interface KiwoomHogaValues {
  /** 호가시간 */
  "21"?: string;

  /** 매도호가 1~10 */
  "41"?: string;
  "42"?: string;
  "43"?: string;
  "44"?: string;
  "45"?: string;
  "46"?: string;
  "47"?: string;
  "48"?: string;
  "49"?: string;
  "50"?: string;

  /** 매도호가수량 1~10 */
  "61"?: string;
  "62"?: string;
  "63"?: string;
  "64"?: string;
  "65"?: string;
  "66"?: string;
  "67"?: string;
  "68"?: string;
  "69"?: string;
  "70"?: string;

  /** 매수호가 1~10 */
  "51"?: string;
  "52"?: string;
  "53"?: string;
  "54"?: string;
  "55"?: string;
  "56"?: string;
  "57"?: string;
  "58"?: string;
  "59"?: string;
  "60"?: string;

  /** 매수호가수량 1~10 */
  "71"?: string;
  "72"?: string;
  "73"?: string;
  "74"?: string;
  "75"?: string;
  "76"?: string;
  "77"?: string;
  "78"?: string;
  "79"?: string;
  "80"?: string;

  /** 매도호가총잔량 */
  "121"?: string;
  /** 매수호가총잔량 */
  "125"?: string;

  /** 예상체결가 */
  "23"?: string;
  /** 예상체결수량 */
  "24"?: string;

  [key: string]: string | undefined;
}

export interface KiwoomRealDataItem {
  type: string; // "0D" for 주식호가잔량
  name: string; // "주식호가잔량"
  item: string; // 종목코드
  values: KiwoomHogaValues;
}

export interface KiwoomWebSocketMessage {
  trnm: string; // "REAL" for live data, "REG" for registration ACK
  data?: KiwoomRealDataItem[];
  return_code?: number;
  return_msg?: string;
}

// ─────────────────────────────────────────────────────────────────────────────
// Hook configuration
// ─────────────────────────────────────────────────────────────────────────────

export interface UseOrderBookConfig {
  /**
   * WebSocket URL of the local Kiwoom bridge server
   * (e.g. "ws://localhost:8766").
   * Used only when `rawMessage` is not provided.
   */
  wsUrl?: string;

  /** 6-digit KRX stock code to subscribe (e.g. "078350"). */
  ticker: string;

  /**
   * Pass a pre-parsed WebSocket message here if your app already manages
   * a global WebSocket connection. The hook will skip its own WS management
   * and parse this message instead.
   */
  rawMessage?: KiwoomWebSocketMessage | null;

  onConnect?: () => void;
  onDisconnect?: () => void;
}
