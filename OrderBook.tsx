import React, { memo, useMemo } from "react";
import { useOrderBook, OrderBookState } from "./useOrderBook";
import { OrderBookRow, UseOrderBookConfig } from "./orderBook.types";
import "./OrderBook.css";

// ─────────────────────────────────────────────────────────────────────────────
// Formatting helpers
// ─────────────────────────────────────────────────────────────────────────────

function fmtPrice(price: number): string {
  if (price === 0) return "—";
  return price.toLocaleString("ko-KR");
}

/** Show as "X.Xk" if >= 1000 for compactness inside narrow cells. */
function fmtVolume(vol: number): string {
  if (vol === 0) return "—";
  if (vol >= 100_000) return `${(vol / 10_000).toFixed(0)}만`;
  if (vol >= 10_000) return `${(vol / 10_000).toFixed(1)}만`;
  return vol.toLocaleString("ko-KR");
}

function fmtTotal(vol: number): string {
  if (vol === 0) return "—";
  return vol.toLocaleString("ko-KR");
}

function fmtHoraTime(raw: string): string {
  if (!raw || raw.length < 6) return "--:--:--";
  return `${raw.slice(0, 2)}:${raw.slice(2, 4)}:${raw.slice(4, 6)}`;
}

// ─────────────────────────────────────────────────────────────────────────────
// Sub-components
// ─────────────────────────────────────────────────────────────────────────────

interface AskRowProps {
  row: OrderBookRow;
  depthPct: number;
}

/**
 * Ask (매도) row — volume on the LEFT, price on the RIGHT.
 * Depth bar grows from the RIGHT edge (price side) outward to the LEFT,
 * matching the Toss Securities MTS visual where the spread is the focal point.
 */
const AskRow = memo<AskRowProps>(({ row, depthPct }) => (
  <div
    className={[
      "ob-row",
      "ob-row--ask",
      row.isFlashing ? "ob-row--flash ob-row--flash-ask" : "",
    ]
      .join(" ")
      .trim()}
    role="row"
    aria-label={`매도 ${row.rank}호가: ${fmtPrice(row.price)}, ${fmtVolume(row.volume)}`}
  >
    {/* Ask volume cell — depth bar anchored to the right (price side) */}
    <div className="ob-cell ob-volume ob-volume--ask">
      <div
        className="ob-depth ob-depth--ask"
        style={{ width: `${depthPct}%` }}
        aria-hidden="true"
      />
      <span className="ob-volume-text ob-volume-text--ask">
        {fmtVolume(row.volume)}
      </span>
    </div>

    {/* Price cell */}
    <div className="ob-cell ob-price ob-price--ask">{fmtPrice(row.price)}</div>

    {/* Empty bid slot — keeps grid alignment */}
    <div className="ob-cell ob-slot-empty" aria-hidden="true" />
  </div>
));
AskRow.displayName = "AskRow";

interface BidRowProps {
  row: OrderBookRow;
  depthPct: number;
}

/**
 * Bid (매수) row — price on the LEFT, volume on the RIGHT.
 * Depth bar grows from the LEFT edge (price side) outward to the RIGHT.
 */
const BidRow = memo<BidRowProps>(({ row, depthPct }) => (
  <div
    className={[
      "ob-row",
      "ob-row--bid",
      row.isFlashing ? "ob-row--flash ob-row--flash-bid" : "",
    ]
      .join(" ")
      .trim()}
    role="row"
    aria-label={`매수 ${row.rank}호가: ${fmtPrice(row.price)}, ${fmtVolume(row.volume)}`}
  >
    {/* Empty ask slot */}
    <div className="ob-cell ob-slot-empty" aria-hidden="true" />

    {/* Price cell */}
    <div className="ob-cell ob-price ob-price--bid">{fmtPrice(row.price)}</div>

    {/* Bid volume cell — depth bar anchored to the left (price side) */}
    <div className="ob-cell ob-volume ob-volume--bid">
      <div
        className="ob-depth ob-depth--bid"
        style={{ width: `${depthPct}%` }}
        aria-hidden="true"
      />
      <span className="ob-volume-text ob-volume-text--bid">
        {fmtVolume(row.volume)}
      </span>
    </div>
  </div>
));
BidRow.displayName = "BidRow";

// ─────────────────────────────────────────────────────────────────────────────
// Spread indicator
// ─────────────────────────────────────────────────────────────────────────────

interface SpreadIndicatorProps {
  bidRatioPct: number;
  bestAsk: number;
  bestBid: number;
}

const SpreadIndicator = memo<SpreadIndicatorProps>(
  ({ bidRatioPct, bestAsk, bestBid }) => {
    const spread = bestAsk > 0 && bestBid > 0 ? bestAsk - bestBid : null;
    return (
      <div className="ob-spread" role="separator" aria-label="호가 스프레드">
        <div className="ob-spread-bar">
          <div
            className="ob-spread-fill ob-spread-fill--ask"
            style={{ width: `${100 - bidRatioPct}%` }}
          />
          <div
            className="ob-spread-fill ob-spread-fill--bid"
            style={{ width: `${bidRatioPct}%` }}
          />
        </div>
        {spread !== null && (
          <span className="ob-spread-label">{fmtPrice(spread)} 스프레드</span>
        )}
      </div>
    );
  }
);
SpreadIndicator.displayName = "SpreadIndicator";

// ─────────────────────────────────────────────────────────────────────────────
// Totals footer
// ─────────────────────────────────────────────────────────────────────────────

interface TotalsRowProps {
  totalAsk: number;
  totalBid: number;
  bidRatioPct: number;
}

const TotalsRow = memo<TotalsRowProps>(({ totalAsk, totalBid, bidRatioPct }) => (
  <div className="ob-totals" role="contentinfo" aria-label="호가 총잔량">
    <div className="ob-total ob-total--ask">
      <span className="ob-total-label">총매도잔량</span>
      <span className="ob-total-value">{fmtTotal(totalAsk)}</span>
    </div>

    <div className="ob-total-ratio">
      <div className="ob-ratio-bar">
        <div
          className="ob-ratio-fill ob-ratio-fill--ask"
          style={{ width: `${100 - bidRatioPct}%` }}
        />
        <div
          className="ob-ratio-fill ob-ratio-fill--bid"
          style={{ width: `${bidRatioPct}%` }}
        />
      </div>
      <div className="ob-ratio-pcts">
        <span className="ob-ratio-pct ob-ratio-pct--ask">
          {(100 - bidRatioPct).toFixed(1)}%
        </span>
        <span className="ob-ratio-pct ob-ratio-pct--bid">
          {bidRatioPct.toFixed(1)}%
        </span>
      </div>
    </div>

    <div className="ob-total ob-total--bid">
      <span className="ob-total-label">총매수잔량</span>
      <span className="ob-total-value">{fmtTotal(totalBid)}</span>
    </div>
  </div>
));
TotalsRow.displayName = "TotalsRow";

// ─────────────────────────────────────────────────────────────────────────────
// Column header
// ─────────────────────────────────────────────────────────────────────────────

const ColumnHeaders = memo(() => (
  <div className="ob-col-headers" role="rowgroup" aria-label="호가창 컬럼 헤더">
    <div className="ob-col-hdr ob-col-hdr--ask">매도잔량</div>
    <div className="ob-col-hdr ob-col-hdr--price">가격</div>
    <div className="ob-col-hdr ob-col-hdr--bid">매수잔량</div>
  </div>
));
ColumnHeaders.displayName = "ColumnHeaders";

// ─────────────────────────────────────────────────────────────────────────────
// Main OrderBook component
// ─────────────────────────────────────────────────────────────────────────────

export interface OrderBookProps extends UseOrderBookConfig {
  /** Display name shown in the header (defaults to ticker). */
  stockName?: string;
  /** Override the pre-connected state from a parent hook. Mutually exclusive with wsUrl. */
  preloadedState?: OrderBookState;
  className?: string;
}

export const OrderBook: React.FC<OrderBookProps> = ({
  wsUrl,
  ticker,
  stockName,
  rawMessage,
  onConnect,
  onDisconnect,
  preloadedState,
  className = "",
}) => {
  // If a parent has already set up the hook, accept its state directly.
  const hookState = useOrderBook({
    wsUrl: preloadedState ? undefined : wsUrl,
    ticker,
    rawMessage: preloadedState ? undefined : rawMessage,
    onConnect,
    onDisconnect,
  });

  const { asks, bids, totalAskVolume, totalBidVolume, horaTime, isConnected } =
    preloadedState ?? hookState;

  // Maximum visible volume — used to scale all depth bars.
  const maxVolume = useMemo(() => {
    const all = [...asks, ...bids].map((r) => r.volume);
    return Math.max(...all, 1);
  }, [asks, bids]);

  // Bid ratio for the spread bar and ratio footer.
  const bidRatioPct = useMemo(() => {
    const total = totalAskVolume + totalBidVolume;
    return total > 0 ? (totalBidVolume / total) * 100 : 50;
  }, [totalAskVolume, totalBidVolume]);

  // Asks: rank 10 at the TOP (furthest from spread), rank 1 at the BOTTOM.
  const sortedAsks = useMemo(
    () => [...asks].sort((a, b) => b.rank - a.rank),
    [asks]
  );

  // Bids: rank 1 at the TOP (closest to spread), rank 10 at the BOTTOM.
  const sortedBids = useMemo(
    () => [...bids].sort((a, b) => a.rank - b.rank),
    [bids]
  );

  const bestAsk = asks.find((r) => r.rank === 1)?.price ?? 0;
  const bestBid = bids.find((r) => r.rank === 1)?.price ?? 0;

  return (
    <div
      className={["ob-container", className].join(" ").trim()}
      role="region"
      aria-label="호가창"
    >
      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <div className="ob-header">
        <div className="ob-header-left">
          <span className="ob-stock-name">{stockName ?? ticker}</span>
          <span
            className={[
              "ob-status",
              isConnected ? "ob-status--live" : "ob-status--disconnected",
            ].join(" ")}
          >
            {isConnected ? "실시간" : "연결 끊김"}
          </span>
        </div>
        <time className="ob-hora-time">{fmtHoraTime(horaTime)}</time>
      </div>

      {/* ── Column headers ─────────────────────────────────────────────────── */}
      <ColumnHeaders />

      {/* ── Book body ──────────────────────────────────────────────────────── */}
      <div className="ob-body" role="table" aria-label="호가 테이블">
        {/* Asks (매도) — rank 10 → 1, top-to-bottom */}
        <div className="ob-side ob-side--ask" role="rowgroup" aria-label="매도 호가">
          {sortedAsks.map((row) => (
            <AskRow
              key={`ask-${row.rank}`}
              row={row}
              depthPct={(row.volume / maxVolume) * 100}
            />
          ))}
        </div>

        {/* Spread indicator */}
        <SpreadIndicator
          bidRatioPct={bidRatioPct}
          bestAsk={bestAsk}
          bestBid={bestBid}
        />

        {/* Bids (매수) — rank 1 → 10, top-to-bottom */}
        <div className="ob-side ob-side--bid" role="rowgroup" aria-label="매수 호가">
          {sortedBids.map((row) => (
            <BidRow
              key={`bid-${row.rank}`}
              row={row}
              depthPct={(row.volume / maxVolume) * 100}
            />
          ))}
        </div>
      </div>

      {/* ── Totals footer ──────────────────────────────────────────────────── */}
      <TotalsRow
        totalAsk={totalAskVolume}
        totalBid={totalBidVolume}
        bidRatioPct={bidRatioPct}
      />
    </div>
  );
};

export default OrderBook;
