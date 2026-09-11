---
name: wqbrain-api-verification-sources
description: How the WorldQuant BRAIN REST contract for D:\wq was established and live-verified, plus the two assumptions the real API disproved
metadata:
  type: reference
---

The WorldQuant BRAIN REST API has no stable published spec, and `D:\wq` started
as an empty directory with no client code to reuse. The contract in
`worldquant/api.py` was first cross-checked against three independent public
implementations on 2026-09-05:

- `github.com/rocky-d/wqb` — `wqb/wqb_urls.py` is a clean endpoint registry;
  README shows the exact `POST /simulations` body. Confirmed Basic Auth login
  returning 201, the `Location` header flow, and `Retry-After` polling.
- `pyworldquant` on PyPI (0.0.2) — `pyworldquant/spot/_simulate.py` confirms
  `status` of `FAIL`/`ERROR`, the `message` field, and `GET /alphas/{id}` →
  `is.checks`.
- `github.com/jdhruv1503/Brainiac` — `Simulation/simulate.py` confirms the full
  `is` block field names (`sharpe`, `fitness`, `turnover`, `returns`,
  `drawdown`, `margin`, `pnl`, `bookSize`, `longCount`, `shortCount`,
  `startDate`, `checks[]`) and the `inquiry` biometric/persona branch on login.

Then **verified end-to-end against the live API on 2026-09-06** with the user's
real account: login → submit → poll → result → yearly stats all confirmed.

Two assumptions the live API disproved (both were caught by graceful
degradation, neither broke the run):

1. **`settings.pasteurization` / `settings.nanHandling` must be the strings
   `"ON"`/`"OFF"`.** None of the three reference repos surfaced this, because
   they build the payload in Python literals rather than loading YAML. YAML 1.1
   parses a bare `ON`/`OFF` as a boolean, which serializes to JSON `true`/`false`
   and earns
   `400 {'settings': {'pasteurization': ['"True" is not a valid choice.']}}`.
   `visualization` is the one setting that genuinely is a boolean.
2. **`GET /alphas/{id}/recordsets/yearly-stats` returns a JSON recordset, not
   CSV**: `{"schema": {"properties": [{"name","type"}...]}, "records": [[...]]}`
   with positional rows — `year, pnl, bookSize, longCount, shortCount, turnover,
   sharpe, returns, drawdown, margin, fitness, stage("IS"/"OS")`. Map columns by
   schema name, never by hard-coded index. `GET /alphas/{id}/recordsets` lists
   all 5 available recordsets (`pnl`, `sharpe`, `turnover`, `daily-pnl`, ...).

Verified later, on 2026-09-07:

3. **`GET /alphas/{id}/check` is asynchronous and is the only source of a resolved
   `SELF_CORRELATION`.** While computing it answers `200` with an empty
   `text/html` body plus `Retry-After`; poll until JSON arrives. Body shape is
   `is.checks[]` (all 8) plus an `is.selfCorrelated` recordset with columns
   `alpha_id, name, instrument_type, region, universe, correlation, sharpe,
   turnover, drawdown, fitness, margin`. `is.checks[].result` takes exactly
   `PASS` / `FAIL` / `PENDING` — and `PENDING` can survive even here, so treat an
   unresolved check as not passed.
4. **`GET /data-fields` filtering is a trap.** `dataset=option9` is **silently
   ignored** (returns all 4367 fields with HTTP 200, no error); the working
   parameter is **`dataset.id=option9`**. `category=option` also works. The
   endpoint returns `400 ["Invalid query"]` (a bare JSON array, not an object)
   unless **both** `universe` and `instrumentType` are present. `limit` caps at
   50 — `limit=200` gives `400 ["Invalid query: pagination limit too high."]`.
   `GET /data-sets?region=USA&delay=1` returns each dataset **once per universe**
   (84 rows for 14 datasets), so dedupe by id.

5. **The yearly-stats `stage` label depends on `testPeriod`.** Without one the rows
   read `IS` / `OS`; with `testPeriod=P1Y` they read **`TRAIN` / `TEST`**. Filtering
   on `IS` alone silently dropped *every* row, so two whole live rounds lost their
   yearly stats while the endpoint kept returning a valid, fully populated
   recordset. This was invisible until the client's warning started quoting the
   response body instead of just its byte count — a "1152 bytes, not parseable"
   message cannot distinguish an empty recordset from a label mismatch. When a
   parse silently yields nothing, log the body.

Still unverified: the `inquiry` captcha branch (not triggered by this account's
login), and whether BRAIN uses rate-limit status codes beyond 429.

**How to apply:** if a BRAIN request shape is ever in question, `worldquant/api.py`'s
module docstring is the authoritative per-endpoint record — update it when the
live API disagrees with it. The two bugs above are the reason `normalize_settings`
coerces YAML booleans and `parse_yearly_stats` reads a schema: do not "simplify"
either back to the naive form.
