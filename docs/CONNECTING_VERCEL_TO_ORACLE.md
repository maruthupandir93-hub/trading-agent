# Connecting your Vercel frontend to your Oracle Cloud backend

A step-by-step guide for this exact setup: **Next.js on Vercel**, **FastAPI on an
Oracle Cloud VM**.

Start with the short answer, then the setup, then the security upgrade.

---

## Short answers

**Do I need to buy a domain?**
**No.** Everything works today with just your Oracle VM's IP address. Skip to
[Part 2](#part-2--the-setup) and you will have a working dashboard.

You only need a domain (or a free substitute) for the **optional** security
upgrade in Part 4, and only before you use **real-money exchange API keys**. For
paper trading and testnet, the IP address is fine.

**Is anything broken right now?**
No. The frontend was rebuilt so the browser only ever talks to Vercel, and Vercel
talks to your Oracle box. That works over plain `http` with no certificate.

---

## Part 1 — What the "http vs https" warning actually meant

I explained the fix without explaining the problem. Here it is properly.

### There are two separate network hops

```
   YOU                    VERCEL                  ORACLE VM              BINANCE
 (browser)  ──hop 1──▶  (Next.js)  ──hop 2──▶    (FastAPI)   ──hop 3──▶
            encrypted              NOT encrypted             encrypted
              ✅                        ⚠️                      ✅
```

**Hop 1 — your browser to Vercel.** Encrypted. Vercel gives every deployment a
free `https://` certificate automatically. Nothing to do.

**Hop 2 — Vercel to your Oracle VM.** **Not encrypted.** Your VM has no
certificate, so it answers on plain `http://140.x.x.x:8000`. The data crosses the
public internet in a form that anyone positioned between Vercel's datacenter and
Oracle's could read.

**Hop 3 — Oracle to Binance.** Encrypted, because Binance has a certificate.

### Why hop 2 was fine before, and is not fine now

Until the last change, hop 2 carried market data — candles, prices, order books.
Public information. If someone read it, nothing was lost; you can get the same
data from Binance for free.

Then you asked me to move the exchange route to the backend. Now hop 2 also
carries **your Binance API key and secret**, because your browser sends them with
each manual order.

Anyone who could read hop 2 would have your exchange credentials. Depending on
that key's permissions, that could mean placing orders or withdrawing funds.

### So what should you do?

| Your situation | What to do |
|---|---|
| Paper trading / testnet keys | **Nothing.** Use it as-is. A testnet key controls no real money. |
| Reading dashboards, charts, agent activity | **Nothing.** All public data. |
| Real Binance/Bybit keys, trade-only permission | Do Part 4 first. |
| Real keys with withdrawal permission | Do Part 4 first — and turn withdrawal permission **off** on the exchange regardless. This app never needs it. |

That is the whole of what I was trying to say.

---

## Part 2 — The setup

### Step 1: Get the backend running on the Oracle VM

SSH into your VM:

```bash
cd ~/trading-agent          # wherever you cloned it
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Create `.env` (see `.env.example` for every option):

```bash
LIVE_TRADING=false
USE_TESTNET=true
DATABASE_URL=postgresql://user:pass@host:5432/dbname
TRADES_API_KEY=pick-a-long-random-string-here
ALLOWED_ORIGINS=https://your-project.vercel.app
```

`TRADES_API_KEY` is a password you invent. It must be **identical** on Vercel and
on the VM — it is what stops strangers from using your backend. Generate one:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Start it:

```bash
.venv/bin/python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

`--host 0.0.0.0` matters. The default `127.0.0.1` accepts connections only from
the VM itself, so Vercel could never reach it.

Check it locally on the VM:

```bash
curl http://localhost:8000/api/monitoring
```

JSON back means the backend is healthy. If this fails, nothing further will work
— fix it here first.

### Step 2: Confirm the VM can reach Binance

This is the test that started all of this. From the VM:

```bash
curl -s http://localhost:8000/api/marketdata/upstream-health | python3 -m json.tool
```

You want:

```json
{
  "upstreams": {
    "binanceSpot":    { "reachable": true, "geoBlocked": false },
    "binanceFutures": { "reachable": true, "geoBlocked": false },
    "yahoo":          { "reachable": true, "geoBlocked": false },
    "fearGreed":      { "reachable": true, "geoBlocked": false }
  },
  "allReachable": true,
  "geoBlocked": [],
  "note": "This host's region is served by every upstream checked."
}
```

If `geoBlocked` lists Binance, your VM's region is blocked too, and moving the
calls off Vercel did not help — you would need to rebuild the VM in a different
Oracle region. Oracle's India, Singapore, Japan and most EU regions are fine;
avoid US regions for this.

### Step 3: Open port 8000 — in BOTH places

This trips up almost everyone, because Oracle Cloud has **two independent
firewalls** and both must allow the port. Opening one and not the other gives a
connection timeout that looks exactly like the backend being down.

**3a. The VCN Security List** (Oracle's cloud firewall)

In the Oracle Cloud console:
Networking → Virtual Cloud Networks → *your VCN* → Subnets → *your subnet* →
Security Lists → *the default list* → **Add Ingress Rules**

| Field | Value |
|---|---|
| Source Type | CIDR |
| Source CIDR | `0.0.0.0/0` |
| IP Protocol | TCP |
| Destination Port Range | `8000` |

**3b. The VM's own firewall** (Linux, inside the machine)

Oracle Linux and Ubuntu images block ports by default even when the security list
allows them.

Oracle Linux / CentOS / Rocky:

```bash
sudo firewall-cmd --permanent --add-port=8000/tcp
sudo firewall-cmd --reload
```

Ubuntu:

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8000 -j ACCEPT
sudo netfilter-persistent save
```

**Verify from your own laptop**, not from the VM:

```bash
curl http://YOUR_VM_PUBLIC_IP:8000/api/monitoring
```

JSON back means both firewalls are open. A hang or "connection refused" means one
of them is not — go back and check both.

### Step 4: Point Vercel at the VM

In the Vercel dashboard: your project → Settings → Environment Variables.

| Name | Value | Notes |
|---|---|---|
| `BACKEND_INTERNAL_URL` | `http://YOUR_VM_PUBLIC_IP:8000` | plain `http` is correct here |
| `TRADES_API_KEY` | the same string as on the VM | must match exactly |
| `DASHBOARD_PASSWORD` | a password of your choice | protects the dashboard itself |

Two things people get wrong:

- **Do not name it `NEXT_PUBLIC_BACKEND_URL`.** Anything starting with
  `NEXT_PUBLIC_` is embedded into the JavaScript sent to browsers. The backend's
  address must stay server-side — see Part 3 for why.
- **`http://` is correct, not `https://`.** Your VM has no certificate. Writing
  `https://` there makes every request fail.

**Redeploy after adding them.** Vercel does not apply new environment variables to
an existing deployment.

### Step 5: Verify the whole chain

Open `https://your-project.vercel.app/health` in a browser. You should see:

```json
{
  "overall": "degraded",
  "checks": [
    { "label": "Postgres",                  "ok": true  },
    { "label": "Trade store",               "ok": true  },
    { "label": "Trading backend",           "ok": true, "detail": "reachable from Vercel" },
    { "label": "binanceSpot (from backend)", "ok": true, "detail": "reachable (HTTP 200)" }
  ]
}
```

The line that matters is **"Trading backend: reachable from Vercel"**. That is
hop 2 working. Each `(from backend)` line is hop 3.

This is deliberately built so the two hops are reported separately, because
"Vercel cannot reach Oracle" and "Oracle cannot reach Binance" look identical from
a broken chart and have completely different fixes.

---

## Part 3 — Why the browser can't talk to Oracle directly

Worth understanding, because it explains a rule you'll otherwise trip over.

Your dashboard is served over `https`. Browsers enforce a rule called
**mixed content**: a page loaded over `https` may not make requests to `http`.
No exceptions, no setting, no header on the server can permit it. The browser
blocks the request before it leaves the machine.

So this is impossible:

```
Browser (https page) ──▶ http://140.x.x.x:8000     ❌ blocked by the browser
```

And this is what we do instead:

```
Browser (https page) ──▶ https://your.vercel.app/api/backend/...   ✅ same origin
                                    │
                                    └──▶ http://140.x.x.x:8000     ✅ server-to-server
```

The second hop is made by Vercel's *server*, and the mixed-content rule applies
only to *browsers*. That is the entire reason your VM needs no certificate for the
dashboard to work.

This is also why WebSockets had to go. A `ws://` connection from an `https` page
is blocked by the same rule, and a WebSocket cannot be relayed through a Vercel
serverless function. Live prices and agent events are polled every 2 seconds
instead. The exchange connection is still real-time — the backend holds a live
Binance socket — only the browser's final hop is polled, so prices are typically
under a second old.

---

## Part 4 — Optional: encrypting hop 2

Only needed before you use **real-money exchange keys**. Three options.

### Option A — Free subdomain + automatic certificate (recommended, no purchase)

[DuckDNS](https://www.duckdns.org) gives you a free subdomain like
`yourname.duckdns.org` pointing at your VM's IP. Sign in with GitHub, no payment.
It supports the TXT records that Let's Encrypt needs, so certificates issue and
renew automatically.

Then [Caddy](https://caddyserver.com) sits in front of your backend and obtains
the certificate for you — it is a web server whose main feature is that HTTPS
requires no configuration.

On the VM:

```bash
# 1. Install Caddy (Ubuntu/Debian)
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install caddy

# 2. Configure it
sudo tee /etc/caddy/Caddyfile > /dev/null <<'EOF'
yourname.duckdns.org {
    reverse_proxy localhost:8000
}
EOF

sudo systemctl restart caddy
```

Open ports **80 and 443** in both firewalls (Step 3 again — Let's Encrypt needs 80
to verify you own the name). You can then **close port 8000** to the internet, so
the backend is reachable only through Caddy.

Update Vercel:

```
BACKEND_INTERNAL_URL=https://yourname.duckdns.org
```

Redeploy. Hop 2 is now encrypted.

> **Do not use `nip.io` or `sslip.io` for this.** They look ideal — `140.x.x.x.nip.io`
> resolves to your IP with no signup — but every user of those services shares a
> single Let's Encrypt rate limit, and it is regularly exhausted. Certificate
> issuance then fails for reasons that have nothing to do with your setup.

### Option B — Your own domain

A `.com` costs roughly $10/year. Point an A record at your VM's IP and use the
same Caddy config with your own hostname. More robust than a free subdomain, and
it also unlocks Option C.

### Option C — Cloudflare Tunnel

Runs `cloudflared` on the VM and needs **no inbound ports open at all** — the VM
dials out to Cloudflare. Good if Oracle's firewall is troublesome.

It **requires a domain** added to Cloudflare for a stable hostname. The
domain-free variant (Quick Tunnels, `*.trycloudflare.com`) generates a **new random
URL every restart** and Cloudflare documents it as not for production, so it is
not suitable here.

### Once you have TLS: getting real-time back

With `https` on the backend, WebSockets become possible again, because `wss://`
from an `https` page is allowed. To switch back:

- `lib/agentEventStream.ts` — poll loop → `new WebSocket(...)` at
  `wss://yourname.duckdns.org/api/dashboard/agent-events`
- `components/MarketData.tsx` — poll loop → a socket to the backend's tick relay

The backend's WebSocket endpoints were never removed and still work. Nothing on
the backend needs to change.

---

## Part 5 — Keeping the backend running

`uvicorn` in an SSH session dies when you disconnect. Use systemd:

```bash
sudo tee /etc/systemd/system/tradingos.service > /dev/null <<'EOF'
[Unit]
Description=TradingOS backend
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/trading-agent
EnvironmentFile=/home/ubuntu/trading-agent/.env
ExecStart=/home/ubuntu/trading-agent/.venv/bin/python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now tradingos
sudo systemctl status tradingos
```

Adjust `User` and the paths for your image (`ubuntu` on Ubuntu, `opc` on Oracle
Linux).

Logs:

```bash
sudo journalctl -u tradingos -f
```

`Restart=always` matters more here than in a normal web app: this process is the
only thing enforcing stop-losses on open positions. It reloads the watch list from
the `monitored_positions` table on startup, so a restart resumes monitoring — but
nothing watches while it is down.

---

## Troubleshooting

Work outward. Each step isolates a different hop.

### The dashboard shows no data at all

```bash
# On the VM — is the backend alive?
curl http://localhost:8000/api/monitoring

# From your laptop — is the port open?
curl http://YOUR_VM_IP:8000/api/monitoring
```

Second one hangs → firewall. Both Step 3a and Step 3b.

### "Could not reach the trading backend at ..."

This is hop 2. In order of likelihood:

1. `BACKEND_INTERNAL_URL` is wrong on Vercel (typo, or `https://` instead of `http://`).
2. You added the variable but did not redeploy.
3. Port 8000 is not open in both firewalls.
4. The backend is not running (`sudo systemctl status tradingos`).

### "... returned 451 ... this location is refused by the provider"

This is hop 3. Your VM's region is blocked by Binance. Check with
`/api/marketdata/upstream-health` and rebuild the VM in a different Oracle region.

### Charts empty but the rest works

```bash
curl "http://YOUR_VM_IP:8000/api/marketdata/candles?symbol=BTCUSDT&type=crypto&interval=1h&limit=5"
```

Real candles back means the backend is fine and the problem is between Vercel and
the browser — check the browser console for blocked requests.

### Prices never update

```bash
curl "http://YOUR_VM_IP:8000/api/marketdata/ticks?binance=btcusdt"
```

The first call always returns `null` — it subscribes, and the first frame takes a
moment. Call it again. Look at `stream.connected` and `stream.lastError`.

### Everything returns 401

`TRADES_API_KEY` differs between Vercel and the VM. They must match exactly. If
you set it on only one side, reads still work and every write fails.

### The browser console shows a blocked `http://` or `ws://` request

A bug — something is bypassing the proxy. Search the codebase for
`serverOnlyBackendUrl` used outside a `.server.ts` file. Components must use
`backendProxyPath()`.

---

## Related documents

- `docs/API_REFERENCE.md` — every backend endpoint, with Postman testing
- `docs/DEPLOYMENT_NETWORKING.md` — the architecture and why it is shaped this way
- `.env.example` — every environment variable, with what it does
- `SETUP-DATABASE.md` — Postgres setup

---

**Sources for the TLS options in Part 4:**
[sslip.io rate limit exhaustion](https://github.com/cunnie/sslip.io/issues/108) ·
[nip.io rate limit reports](https://github.com/microsoft/aksworkshop/issues/29) ·
[DuckDNS + Let's Encrypt](https://www.duckdns.org) ·
[Cloudflare Quick Tunnels are not for production](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/)
