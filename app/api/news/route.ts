// Crypto/markets headlines: RSS feeds from the BACKEND, plus optional keyed
// aggregator APIs from here.
//
// WHY THE RSS HALF MOVED TO THE BACKEND
//
// The feed list includes `www.binance.com/en/support/announcement/rss`. A Binance
// host refuses a restricted region with the same 451 that broke /api/candles, so
// on Vercel this route carried the identical latent failure — and in its harder
// form: a dead feed returned `[]`, so the panel showed FEWER headlines rather
// than an error. Nobody would have filed a bug for that.
//
// The backend fetches and parses all seven feeds from a served region and reports
// each one's outcome separately, so a missing source is visible as a missing
// source. See backend/api/marketdata.py::get_news.
//
// WHY THE KEYED AGGREGATORS DID NOT MOVE
//
// APITube / GNews / NewsX / NewsData are not geo-restricted, their API keys live
// in this deployment's environment, and the daily-usage counter that rotates
// between them is a `.data/` store on this side (lib/newsProviderUsage.server.ts).
// Moving them would mean migrating a secret and splitting that store across two
// deployments — a separate and deliberate operation from fixing a geo-block, and
// one with no benefit here.
//
// None of them are required: with zero keys configured this route returns the
// full RSS list, which is the current state of this deployment.
//
// Response shape is unchanged: { items, aggregatorUsed, aggregatorNote }.

import { NEWS_AGGREGATOR_PROVIDERS, pickAvailableProvider, type NewsProviderMeta } from '@/lib/newsProviders';
import { getUsageToday, incrementUsage } from '@/lib/newsProviderUsage.server';
import { fetchFromBackend } from '@/lib/api/backendProxy.server';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

type NewsItem = { title: string; link: string; source: string; pubDate: string | null };

type BackendNews = {
  items: NewsItem[];
  sources: { source: string; available: boolean; error: string | null; count: number }[];
  availableCount: number;
  totalCount: number;
};

// Each aggregator has its own request shape and response shape — this
// is the one place that translates "some provider, some API key" into
// the same NewsItem[] shape the rest of the app already understands.
// Wrapped in try/catch per-provider so one bad response doesn't take
// down the whole route; RSS results still come back regardless.
async function fetchFromAggregator(provider: NewsProviderMeta, apiKey: string, query: string): Promise<NewsItem[]> {
  try {
    if (provider.id === 'apitube') {
      const res = await fetch(`https://api.apitube.io/v1/news/everything?title=${encodeURIComponent(query)}&per_page=10&api_key=${apiKey}`);
      if (!res.ok) return [];
      const json = await res.json();
      const results = json?.results ?? [];
      return results.map((r: { title?: string; href?: string; source?: { domain?: string }; published_at?: string }) => ({
        title: r.title ?? '',
        link: r.href ?? '',
        source: r.source?.domain ?? 'APITube',
        pubDate: r.published_at ?? null,
      })).filter((i: NewsItem) => i.title && i.link);
    }
    if (provider.id === 'gnews') {
      const res = await fetch(`https://gnews.io/api/v4/search?q=${encodeURIComponent(query)}&max=10&apikey=${apiKey}`);
      if (!res.ok) return [];
      const json = await res.json();
      const articles = json?.articles ?? [];
      return articles.map((a: { title?: string; url?: string; source?: { name?: string }; publishedAt?: string }) => ({
        title: a.title ?? '',
        link: a.url ?? '',
        source: a.source?.name ?? 'GNews',
        pubDate: a.publishedAt ?? null,
      })).filter((i: NewsItem) => i.title && i.link);
    }
    if (provider.id === 'newsx') {
      const res = await fetch(`https://api.newsx.dev/v1/news?query=${encodeURIComponent(query)}&limit=10&apikey=${apiKey}`);
      if (!res.ok) return [];
      const json = await res.json();
      const items = json?.articles ?? json?.data ?? [];
      return items.map((a: { title?: string; url?: string; source?: string; publishedAt?: string }) => ({
        title: a.title ?? '',
        link: a.url ?? '',
        source: a.source ?? 'NewsX',
        pubDate: a.publishedAt ?? null,
      })).filter((i: NewsItem) => i.title && i.link);
    }
    if (provider.id === 'newsdata') {
      const res = await fetch(`https://newsdata.io/api/1/news?apikey=${apiKey}&q=${encodeURIComponent(query)}&language=en`);
      if (!res.ok) return [];
      const json = await res.json();
      const results = json?.results ?? [];
      return results.map((r: { title?: string; link?: string; source_id?: string; pubDate?: string }) => ({
        title: r.title ?? '',
        link: r.link ?? '',
        source: r.source_id ?? 'NewsData.io',
        pubDate: r.pubDate ?? null,
      })).filter((i: NewsItem) => i.title && i.link);
    }
    return [];
  } catch {
    return [];
  }
}

export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  // Optional query param to bias the aggregator search (e.g. "BTC") —
  // RSS feeds always return their general feed regardless, since they
  // don't support server-side filtering by keyword.
  const query = searchParams.get('q') ?? 'crypto';

  let feedSources: BackendNews['sources'] = [];

  try {
    // RSS via the backend. A failure here is NOT fatal: the aggregator half may
    // still have something, and an empty news panel is a worse answer than a
    // partial one as long as the shortfall is reported in `feedSources`.
    let items: NewsItem[] = [];
    try {
      const backendNews = await fetchFromBackend<BackendNews>('/api/marketdata/news', { limit: '40' });
      items = backendNews.items ?? [];
      feedSources = backendNews.sources ?? [];
    } catch (err) {
      feedSources = [{
        source: 'RSS (backend)',
        available: false,
        error: err instanceof Error ? err.message : 'backend unreachable',
        count: 0,
      }];
    }

    let aggregatorUsed: string | null = null;
    let aggregatorNote: string;

    const configuredKeys = new Set(
      NEWS_AGGREGATOR_PROVIDERS.map((p) => p.envKeyName).filter((key) => !!process.env[key]),
    );
    const usageToday = await getUsageToday();
    const provider = pickAvailableProvider(NEWS_AGGREGATOR_PROVIDERS, configuredKeys, usageToday);

    if (provider) {
      const apiKey = process.env[provider.envKeyName] as string;
      const aggregatorItems = await fetchFromAggregator(provider, apiKey, query);
      if (aggregatorItems.length > 0) {
        items = [...aggregatorItems, ...items];
        await incrementUsage(provider.id);
        aggregatorNote = `${provider.name} (${(usageToday[provider.id] ?? 0) + 1}/${provider.dailyLimit} used today)`;
        aggregatorUsed = provider.id;
      } else {
        aggregatorNote = `${provider.name} was selected but returned no results this call — RSS-only for this response`;
      }
    } else if (configuredKeys.size === 0) {
      aggregatorNote = 'No aggregator API keys configured — RSS feeds only (still free, no limit)';
    } else {
      aggregatorNote = 'All configured aggregator providers have hit their daily free-tier limit — RSS feeds only until they reset (UTC midnight)';
    }

    items.sort((a, b) => {
      const ta = a.pubDate ? Date.parse(a.pubDate) : 0;
      const tb = b.pubDate ? Date.parse(b.pubDate) : 0;
      return tb - ta;
    });

    // `feedSources` is additive — existing consumers read `items` and are
    // unaffected. It exists so a panel can say WHICH feed is missing instead of
    // silently rendering a shorter list.
    return Response.json({ items, aggregatorUsed, aggregatorNote, feedSources });
  } catch (err) {
    const message = err instanceof Error ? err.message : 'unknown error';
    return Response.json({ error: `Could not fetch news: ${message}` }, { status: 502 });
  }
}
