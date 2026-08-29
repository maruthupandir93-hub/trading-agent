'use client';

import { createContext, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { loadLS, saveLS, LS_KEYS } from '@/lib/storage';
import { DEFAULT_WATCHLIST } from '@/lib/constants';
import type { Tick, WatchItem } from '@/lib/types';

type FlashDir = 'up' | 'down' | null;

type MarketDataValue = {
  watchlist: WatchItem[];
  setWatchlist: (updater: WatchItem[] | ((w: WatchItem[]) => WatchItem[])) => void;
  ticks: Record<string, Tick>;
  flash: Record<string, FlashDir>;
  quoteApiError: string | null;
};

const MarketDataContext = createContext<MarketDataValue | null>(null);

export function useMarketData(): MarketDataValue {
  const ctx = useContext(MarketDataContext);
  if (!ctx) throw new Error('useMarketData must be used within MarketDataProvider');
  return ctx;
}

const EQUITY_POLL_MS = 8000;

// Crypto ticks are polled far more often than equities because the backend
// already holds them in memory from a live socket — this poll is a cache read,
// not an exchange round-trip, so 2s costs almost nothing and keeps the price
// grid feeling live. Equities genuinely hit Yahoo per poll, hence 8s.
//
// This is the one place the loss from retiring the browser's own WebSocket is
// visible: ticks are now up to CRYPTO_POLL_MS old instead of instant. That is
// the unavoidable price of the browser only being allowed to talk to its own
// https origin — see the effect below for why.
const CRYPTO_POLL_MS = 2000;
const FLASH_MS = 700;

export function MarketDataProvider({ children }: { children: React.ReactNode }) {
  const [watchlist, setWatchlistState] = useState<WatchItem[]>(DEFAULT_WATCHLIST);
  const [ticks, setTicks] = useState<Record<string, Tick>>({});
  const [flash, setFlash] = useState<Record<string, FlashDir>>({});
  const [quoteApiError, setQuoteApiError] = useState<string | null>(null);
  const [hydrated, setHydrated] = useState(false);
  const flashTimers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const simTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    // Local copy first so the first render has a list; the server corrects it if
    // it has one. A `null` watchlist from the server means NO DATABASE, not an
    // empty list — replacing the local list with [] there would silently clear it.
    setWatchlistState(loadLS<WatchItem[]>(LS_KEYS.watchlist, DEFAULT_WATCHLIST));
    fetch('/api/watchlist')
      .then((res) => res.json())
      .then((json: { watchlist: WatchItem[] | null }) => {
        if (Array.isArray(json.watchlist) && json.watchlist.length > 0) {
          setWatchlistState(json.watchlist);
          saveLS(LS_KEYS.watchlist, json.watchlist);
        }
      })
      .catch(() => {});
    setHydrated(true);
  }, []);

  useEffect(() => {
    if (!hydrated) return;
    saveLS(LS_KEYS.watchlist, watchlist);
    // Mirrored to Postgres so the list is not confined to this browser. Logged,
    // not surfaced, on failure: the local copy is already authoritative here.
    fetch('/api/watchlist', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ watchlist }),
    }).catch(() => {});
  }, [watchlist, hydrated]);

  function setWatchlist(updater: WatchItem[] | ((w: WatchItem[]) => WatchItem[])) {
    setWatchlistState((prev) => (typeof updater === 'function' ? (updater as (w: WatchItem[]) => WatchItem[])(prev) : updater));
  }

  function applyTick(symbol: string, price: number, prevClose: number | null, source: Tick['source']) {
    setTicks((prev) => {
      const before = prev[symbol];
      if (before && before.price !== price) {
        const dir: FlashDir = price > before.price ? 'up' : 'down';
        setFlash((f) => ({ ...f, [symbol]: dir }));
        clearTimeout(flashTimers.current[symbol]);
        flashTimers.current[symbol] = setTimeout(() => {
          setFlash((f) => ({ ...f, [symbol]: null }));
        }, FLASH_MS);
      }
      return { ...prev, [symbol]: { price, prevClose, ts: Date.now(), source } };
    });
  }

  const cryptoItems = useMemo(() => watchlist.filter((w) => w.type === 'crypto' && w.binance), [watchlist]);
  const equityItems = useMemo(() => watchlist.filter((w) => w.type === 'equity'), [watchlist]);

  // --- Crypto: poll /api/ticks, which is fed by the BACKEND's Binance socket ---
  //
  // THIS USED TO BE A WEBSOCKET THIS COMPONENT OPENED ITSELF, straight to
  // `wss://stream.binance.com:9443`, one per visitor.
  //
  // Two reasons it moved, and the second is the one that actually forced it:
  //
  //  1. It made the dashboard's correctness depend on the VIEWER's location.
  //     Binance refuses some regions, so an operator in one saw a price grid
  //     that silently never ticked — from the same build that worked elsewhere.
  //     It is also why live prices kept updating while /api/candles returned
  //     502: the two took completely different routes to the same exchange, and
  //     that masked how broken the data layer was.
  //  2. Every other market call now goes through the backend so that Vercel's
  //     region cannot refuse it. Leaving one browser-to-exchange socket behind
  //     would leave one path that fails for a reason none of the others can.
  //
  // WHY POLLING RATHER THAN A SOCKET TO OUR OWN BACKEND: this page is served
  // over https and the backend has no TLS certificate, so the browser refuses
  // `ws://` outright (mixed content) and a WebSocket cannot be proxied through a
  // Vercel serverless function. The browser's last hop therefore has to be an
  // ordinary same-origin https request.
  //
  // ONLY THAT LAST HOP IS POLLED. The backend still holds a real-time socket to
  // Binance, so what a poll reads is a second or so old — not a fresh REST
  // round-trip per symbol. The tick's own `ageSeconds` is carried through so
  // staleness is measured, not assumed.
  useEffect(() => {
    if (!hydrated || cryptoItems.length === 0) return;
    let cancelled = false;

    const bySlug = new Map(cryptoItems.map((w) => [w.binance!.toLowerCase(), w.symbol]));
    const query = [...bySlug.keys()].join(',');

    async function poll() {
      try {
        const res = await fetch(`/api/ticks?binance=${encodeURIComponent(query)}`);
        const json = await res.json();
        if (cancelled) return;
        if (json.error) throw new Error(json.error);

        for (const [slug, tick] of Object.entries(json.ticks ?? {})) {
          // null means "subscribed, but no frame has arrived yet" — a symbol
          // asked for the first time. Skipped rather than rendered as zero.
          if (!tick) continue;
          const symbol = bySlug.get(slug);
          if (!symbol) continue;
          const t = tick as { price: number; prevClose: number | null; stale: boolean };
          if (!Number.isFinite(t.price)) continue;
          // Still labelled 'ws-live': the tick genuinely originated from a
          // websocket, just the backend's rather than this browser's, and the
          // provenance labels exist to describe the DATA's origin, not the
          // transport of the final hop. A stale one is downgraded so the UI's
          // freshness indicator stays truthful.
          applyTick(symbol, t.price, t.prevClose ?? null, t.stale ? 'poll-live' : 'ws-live');
        }
      } catch {
        // Silent: the equity poll below owns the visible error banner, and a
        // transient failure here self-corrects on the next tick. A hard outage
        // shows up as prices going stale, which the UI already surfaces.
      }
    }

    poll();
    const iv = setInterval(poll, CRYPTO_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hydrated, cryptoItems.map((c) => c.binance).join(',')]);

  // --- Equities: poll the server-side quote route ---
  useEffect(() => {
    if (!hydrated || equityItems.length === 0) return;
    let cancelled = false;

    async function poll() {
      try {
        const symbols = equityItems.map((e) => e.symbol).join(',');
        const res = await fetch(`/api/quote?symbols=${encodeURIComponent(symbols)}`);
        const json = await res.json();
        if (cancelled) return;
        if (json.error) throw new Error(json.error);
        setQuoteApiError(null);
        for (const q of json.quotes ?? []) {
          if (typeof q.price === 'number') applyTick(q.symbol, q.price, q.prevClose ?? null, 'poll-live');
        }
      } catch (err) {
        if (!cancelled) setQuoteApiError(err instanceof Error ? err.message : 'quote fetch failed');
      }
    }
    poll();
    const iv = setInterval(poll, EQUITY_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hydrated, equityItems.map((e) => e.symbol).join(',')]);

  // --- Honest simulated fallback: only for symbols with NO real tick at all
  // after 10s (equity poll failed and it's not a crypto symbol either), so
  // the UI doesn't sit blank — clearly labeled 'sim-fallback' everywhere,
  // including to the model in buildLiveMarketContext.
  useEffect(() => {
    if (!hydrated) return;
    simTimer.current && clearInterval(simTimer.current);
    simTimer.current = setInterval(() => {
      setTicks((prev) => {
        const next = { ...prev };
        let changed = false;
        for (const w of watchlist) {
          const t = prev[w.symbol];
          const stale = !t || Date.now() - t.ts > 12000;
          if (stale && (w.type !== 'equity' || quoteApiError)) {
            const base = t?.price ?? (w.type === 'crypto' ? 50000 : 100);
            const walk = base * (1 + (Math.random() - 0.5) * 0.002);
            next[w.symbol] = { price: walk, prevClose: t?.prevClose ?? base, ts: Date.now(), source: 'sim-fallback' };
            changed = true;
          }
        }
        return changed ? next : prev;
      });
    }, 4000);
    return () => {
      simTimer.current && clearInterval(simTimer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hydrated, watchlist, quoteApiError]);

  const value: MarketDataValue = { watchlist, setWatchlist, ticks, flash, quoteApiError };
  return <MarketDataContext.Provider value={value}>{children}</MarketDataContext.Provider>;
}
