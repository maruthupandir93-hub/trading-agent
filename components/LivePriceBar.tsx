'use client';

// A slim, ALWAYS-VISIBLE live price strip for the three instruments the operator
// watches — BTC, ETH, SOL. It is mounted in the ROOT layout (app/layout.tsx),
// above {children}, so Next keeps it mounted across every page navigation: the
// prices stay on screen and keep ticking wherever the operator goes, instead of
// re-mounting and blanking each time a page changes.
//
// It fetches its OWN data from /api/ticks (the backend's Binance socket cache) —
// deliberately not through MarketData context — so it is independent of which
// providers a given page mounts and cannot be blanked by a page that does not use
// them. Asking for a symbol also SUBSCRIBES to it on the backend, so this keeps the
// three majors warm for every other view too.
//
// Same last-hop-polling rule as the rest of the app: the site is https and the
// backend has no TLS cert, so the browser cannot open a ws:// socket — see
// app/api/ticks/route.ts. The exchange socket behind the cache is still real-time.

import { useEffect, useRef, useState } from 'react';

type Row = { label: string; slug: string; price: number | null; dir: 'up' | 'down' | null };

const INSTRUMENTS: { label: string; slug: string }[] = [
  { label: 'BTC', slug: 'btcusdt' },
  { label: 'ETH', slug: 'ethusdt' },
  { label: 'SOL', slug: 'solusdt' },
];

const POLL_MS = 3000;

function fmt(price: number): string {
  // More decimals for cheaper coins so SOL does not render as a bare integer.
  const digits = price >= 1000 ? 0 : price >= 1 ? 2 : 4;
  return price.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function LivePriceBar() {
  const [rows, setRows] = useState<Row[]>(
    INSTRUMENTS.map((i) => ({ ...i, price: null, dir: null })),
  );
  // Last price per slug, to colour the next tick up/down without re-rendering on it.
  const lastRef = useRef<Record<string, number>>({});

  useEffect(() => {
    let cancelled = false;
    const query = INSTRUMENTS.map((i) => i.slug).join(',');

    async function poll() {
      try {
        const res = await fetch(`/api/ticks?binance=${encodeURIComponent(query)}`, {
          cache: 'no-store',
        });
        const json = await res.json();
        if (cancelled || json?.error) return;
        const ticks = (json?.ticks ?? {}) as Record<string, { price?: number } | null>;

        setRows((prev) =>
          prev.map((r) => {
            const t = ticks[r.slug];
            const price = t && Number.isFinite(t.price) ? (t.price as number) : null;
            if (price === null) return r; // keep the last good price rather than blanking
            const last = lastRef.current[r.slug];
            const dir: Row['dir'] = last === undefined || last === price ? r.dir : price > last ? 'up' : 'down';
            lastRef.current[r.slug] = price;
            return { ...r, price, dir };
          }),
        );
      } catch {
        // Silent and self-correcting: a transient failure just keeps the last
        // price on screen until the next poll succeeds.
      }
    }

    void poll();
    const id = setInterval(() => void poll(), POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  return (
    <div
      style={{
        position: 'sticky',
        top: 0,
        zIndex: 50,
        display: 'flex',
        alignItems: 'center',
        gap: 20,
        padding: '4px 12px',
        fontSize: 12,
        borderBottom: '1px solid var(--border, #2a2a2a)',
        background: 'var(--bg-surface, #111)',
        color: 'var(--text-secondary, #aaa)',
        overflowX: 'auto',
        whiteSpace: 'nowrap',
      }}
    >
      <span style={{ opacity: 0.6, textTransform: 'uppercase', letterSpacing: '0.08em', fontSize: 10 }}>
        Live
      </span>
      {rows.map((r) => (
        <span key={r.slug} style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
          <span style={{ opacity: 0.7, fontWeight: 600 }}>{r.label}</span>
          <span
            className="mono"
            style={{
              fontWeight: 600,
              color:
                r.price === null
                  ? 'var(--text-muted, #666)'
                  : r.dir === 'up'
                    ? 'var(--success, #16a34a)'
                    : r.dir === 'down'
                      ? 'var(--danger, #dc2626)'
                      : 'var(--text-primary, #eee)',
            }}
          >
            {r.price === null ? '—' : `$${fmt(r.price)}`}
          </span>
        </span>
      ))}
    </div>
  );
}
