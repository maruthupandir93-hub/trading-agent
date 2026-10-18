# "TRADES_API_KEY is not configured, so writes are refused"

If the dashboard renders but every button is refused with

```json
{"detail":"This endpoint changes state and TRADES_API_KEY is not configured,
 so writes are refused. ..."}
```

then the backend is working exactly as designed and **two environment
variables are missing**. Nothing is broken and nothing needs rolling back.

---

## Why it happens

Making the backend fail CLOSED was finding **F6** of the October diagnosis.
Before it, `require_write_auth` was a complete no-op whenever
`TRADES_API_KEY` was unset, and the live backend said so in its own status:

```
"writeAuthEnabled": false,
"note": "TRADES_API_KEY is not set, so ALL endpoints are open."
```

Port 8000 on the VM is reachable from the internet. So anyone who found it
could enable live trading, switch venue, start or stop a session, reset the
paper book, or place a real order through `/api/operator/exchange/order` — with
no credential at all. An auth dependency that passes when it is not configured
fails **open**, which is the wrong direction for a system that moves money.

It fails closed now. **Reads are still open**, deliberately — the browser polls
them directly and cannot hold a server secret without shipping it to every
client — which is why the dashboard renders perfectly while nothing on it works.

## There are TWO gates, and both refuse when unset

Fixing one and not the other leaves an identical-looking refusal, which is the
loop worth avoiding:

| Variable | Lives on | Unset → | Refused by |
|---|---|---|---|
| `DASHBOARD_PASSWORD` | the **frontend** (Vercel) | reads pass, writes **401** | `app/api/backend/[...path]/route.ts` |
| `TRADES_API_KEY` | the **backend** *and* the frontend | writes **503** | `backend/core/auth.py` |

`TRADES_API_KEY` is needed in **both** places: the backend checks it, and the
Next proxy is what *attaches* it to each forwarded request. Set it on the
backend only and the proxy still sends nothing, so the 503 does not change.

The browser never holds either secret. The proxy adds the API key server-side,
and the dashboard password is a Basic credential the browser replays
automatically after the middleware challenges it once.

---

## The fix, in order

### 1. Generate one value for each

```bash
python -c "import secrets;print(secrets.token_urlsafe(32))"   # TRADES_API_KEY
python -c "import secrets;print(secrets.token_urlsafe(18))"   # DASHBOARD_PASSWORD
```

Use the **same** `TRADES_API_KEY` string in all three places below. A mismatch
fails exactly like an absence.

### 2. The backend (Oracle VM)

`docker-compose.yml` reads `env_file: .env`, so this is one file and a restart:

```bash
cd ~/trading-agent        # wherever the repo is checked out
echo 'TRADES_API_KEY=<the key>' >> .env
docker compose up -d --force-recreate trading_os
```

Confirm it took — this endpoint is a **read**, so it answers either way:

```bash
curl -s http://localhost:8000/api/admin/status | python -m json.tool | grep -A6 '"auth"'
```

You want `"writeAuthEnabled": true` and `"writesPossible": true`.

### 3. The dashboard (Vercel)

Project → Settings → Environment Variables, for the Production environment:

- `TRADES_API_KEY` = the same value as step 2
- `DASHBOARD_PASSWORD` = the password from step 1

Then **redeploy** — Vercel bakes environment variables at build time, so saving
them without a redeploy changes nothing.

### 4. Check

Reload the dashboard. The browser asks for a password once (any username; the
password is what is checked). The red **"Writes disabled"** banner at the top of
Settings disappears, and the *API auth* card reads **Write auth on**.

---

## The local-only escape hatch, and when NOT to use it

`ALLOW_UNAUTHENTICATED_WRITES=true` restores the old open behaviour. It exists
for a backend that is genuinely unreachable from anywhere but localhost.

**Do not set it on the Oracle VM.** Port 8000 there is internet-reachable, so it
re-opens exactly the hole F6 closed: live trading, venue switching and real
order placement, available to anyone who finds the host. The status endpoint
reports it as a warning rather than a pass for that reason, and the dashboard
never offers it as an option.

---

## What the dashboard does about it now

Three changes, so this is visible before a click rather than after one:

- **The blocker renders above the controls** on Settings, naming the variable
  and the three steps — the same rule `VenueSwitchPanel` already follows, since
  discovering a refusal from a toast is worse than never being offered the
  action.
- **`lib/writeBlocker.ts` rewrites the refusal** wherever one is still shown. It
  rewrites only the two refusals with a known, fixable cause and leaves every
  other one exactly as the backend worded it — flattening a 409 *"a real
  position is open"* into advice about environment variables would be a worse
  bug than the one being fixed. It also does **not** repeat the backend's
  mention of `ALLOW_UNAUTHENTICATED_WRITES`, for the reason above.
- **The emergency stop names the likely cause.** A bare `HTTP 503` on that
  button sends you looking for an outage at the moment you most need to act. It
  still states plainly that the system is **NOT** stopped, and says to stop the
  agent at the exchange if you need it stopped right now.

## One thing this exposed in the test suite

Writing a real `TRADES_API_KEY` into `.env` turned three `test_api_surface`
tests red at once. They were not a regression: they POST to
`/api/knowledge/relationship` with no credential and assert `200`, and they had
passed for years **because nothing was configured**, not because they had
isolated anything.

That is the same lesson this repo already records for `LLM_*` — where two tests
asserting "no provider is configured" started issuing live HTTP requests the
moment a real key went into `.env`, and only the network guard caught it.

`tests/conftest.py::isolate_write_auth` now clears `TRADES_API_KEY` and
`DASHBOARD_PASSWORD` for every test and pins `ALLOW_UNAUTHENTICATED_WRITES`
explicitly, so the suite runs a **known** configuration rather than whatever the
operator's `.env` happens to hold. `tests/test_api_auth.py` owns the real
behaviour and sets both variables itself, so the refusal is still exercised in
both directions.

---

And the status endpoint stopped making a false claim. With no key it used to
report *"ALL endpoints are open"* — true while the dependency was a no-op, and
the exact opposite of the truth once writes failed closed.
`tests/test_diagnosis_p0.py` pins that it can never say that again.
