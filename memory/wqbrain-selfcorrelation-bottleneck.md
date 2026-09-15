---
name: wqbrain-selfcorrelation-bottleneck
description: SELF_CORRELATION was the binding constraint. Analyst theme alone maxes at self_corr ~0.85 against 2rOn70lb. The breakthrough (2026-09-09) is blending analyst (3 parts) with an option8 skew_180 leg (1 part): fitness stays EXCELLENT (2.25) while self_corr drops to 0.6857. Includes the hypothesis about which alphas BRAIN actually compares against
metadata:
  type: project
---

On 2026-09-07 all ten stored GOOD-or-better alphas were re-checked against the
live `GET /alphas/{id}/check` endpoint. **Only one was submittable**:
`2rOn70lb` (SPECTACULAR, fitness 2.69, 8/8 PASS, self-correlation 0.6996 against
a 0.7 limit). The other nine failed, eight of them on `SELF_CORRELATION` alone:

- `npKzLemd` / `YP5Wj6Rl` (both EXCELLENT, fitness 2.07) correlate at **exactly
  1.0** — they are the same expression run under two `truncation` values.
- `Vk6YvwW8` 0.863, `O0r5pPgq` 0.8497, `kqVL1PLd` 0.8425, `58QqvJkz` 0.7358,
  `Vk6Y8xXM` 0.7206 — all the analyst earnings-yield recipe variants.
- `om6zPKjn` / `mLgzK5zW` fail outright on `LOW_SHARPE` (0.91/0.90) and
  `LOW_SUB_UNIVERSE_SHARPE` (0.22/0.24); `mLgzK5zW` also on `LOW_TURNOVER`
  (0.0089). These are the two degenerate long-only books, and the checks confirm
  the signal was never in stock selection.

An earlier README claim that `Vk6YvwW8` had "checks 全过" was **wrong** and has
been corrected: it read the alpha payload's `checks`, where `SELF_CORRELATION`
is `PENDING` forever, and treated PENDING as fine.

**Why:** the account already contained a SPECTACULAR alpha built on "analyst
earnings yield + consensus net profit". Every new candidate on that same theme
collided with the 0.7 correlation ceiling, so raising fitness further on the
existing recipe could not produce another submittable alpha. Fitness was the
constraint up to round 13; correlation became it after.

**Resolved (rounds 15-21): the theme switch works.** Moving to `option8` (implied
volatility) plus `pv13` (relationship graph — competitor return spillovers)
produced a long run of 8/8-PASS alphas and, at round 21, the first GOOD:

- **`Vk627oWY` (`r21_t1060`), GOOD, 8/8 PASS** — sharpe 1.84, fitness 1.53,
  turnover 11.2%, returns 8.6%, drawdown 7.0%, book 1496/1363, train 1.90 →
  **test 1.58 (83% retention)**, 4/4 positive years with worst year 1.16, and an
  **empty `selfCorrelated` recordset**. Recipe: IV term structure
  `implied_volatility_mean_10 / _60`, weighted 2:1:1 with `rel_ret_comp` (10d) and
  `implied_volatility_mean_skew_360` (20d), each leg `group_zscore`d separately.
- Its 83% out-of-sample retention is the best of any GOOD-or-better alpha found;
  the SPECTACULAR `2rOn70lb` retains only 27% (2.62 → 0.71). By held-out-year
  performance `Vk627oWY` is the more trustworthy alpha even at half the fitness.
- fitness 1.53 clears the GOOD boundary (1.52) by 0.01 — the grade is BRAIN's own
  field, not a local inference, but the margin is thin.
- 36 of 183 completed simulations are 8/8 PASS; only 2 of those are GOOD or better.

**Continued (rounds 24-25, 2026-09-07 → 2026-09-08): the option8 × pv13 theme
kept producing GOOD+submittable alphas once the returns-reversal pv1 leg was
grafted in.** Three more GOOD / 8/8-PASS alphas were found after the snapshot
above, all in the same family and all using the same `a + a + ...` 2×-weighted
IV-term-structure recipe on `industry`:

- **`r24_term_skew_rev` (`2rOamR1P`?), GOOD, 8/8 PASS** (2026-09-07) — sharpe
  1.75, fitness 1.53, turnover 11.6%, returns 9.5%, drawdown 9.6%, book
  balanced, 4/4 positive years (worst 0.84). Recipe: IV term
  `_10/_60` (2×) + `implied_volatility_mean_skew_360` (20d) + **returns
  reversal** `−ts_mean(returns, 5)`, each leg `group_zscore`d. Replaces the
  `rel_ret_comp` (pv13) leg of `r21_t1060` with a pv1 reversal leg — same
  family, different dataset on the second leg, satisfies constraint 7
  (mix data categories inside one alpha).
- **`r25_term_skew180_rev` (`MP1pYp0o`), GOOD, 8/8 PASS** (2026-09-08) —
  sharpe 1.76, fitness 1.56, turnover 11.4%, returns 9.9%, drawdown 11.1%,
  4/4 positive years (worst 0.76), **self-correlation 0.0107** (lowest ever
  measured for this account, well under the 0.7 limit). Recipe: same as
  `r24_term_skew_rev` but with `implied_volatility_mean_skew_180` (20d)
  instead of `_360`. The skew_180 leg was the strongest standalone skew in
  the r22 probe (sharpe 1.02), and that strength carried into the blend —
  this is the **highest-fitness alpha in the option8 × pv13 family**.
- **`r25_term_skew90_rev` (`GrdwWoXQ`), GOOD, 8/8 PASS** (2026-09-08) —
  sharpe 1.71, fitness 1.52, turnover 11.4%, returns 9.9%, drawdown 12.3%,
  4/4 positive years (worst 0.79), train 1.73 → test 1.65 (95% retention —
  best of any GOOD+ alpha including `r21_t1060`'s 83%). Recipe: same as
  `r25_term_skew180_rev` but with `implied_volatility_mean_skew_90` (20d).
  The skew_90 leg was the second-strongest standalone skew in r22 (0.96).

Two consequences for the search strategy:

1. **The skew-tenor sweep is real signal, not noise.** Every skew tenor
   probed standalone (skew_90 0.96, skew_180 1.02, skew_360 ~1.0) produced
   a GOOD alpha when blended with the IV-term + returns-reversal legs, and
   the differences in fitness track the standalone strengths (1.02 → 1.56,
   0.96 → 1.52). When the standalone probe says a leg is strong, the blend
   inherits that strength — keep probing legs standalone first.
2. **`SELF_CORRELATION` is still not the binding constraint in this family.**
   `MP1pYp0o` correlates at **0.0107** against the existing account,
   including against `r21_t1060` and `r24_term_skew_rev` which share 3 of
   its 4 legs. That confirms the r23 finding: within the option8 × pv13
   family, near-duplicate recipes do not flag one another. **Fitness is.**
   The family has now produced 4 distinct GOOD+submittable alphas
   (`r21_t1060`, `r24_term_skew_rev`, `r25_term_skew180_rev`,
   `r25_term_skew90_rev`); the fitness ceiling for this leg combination
   looks to be ~1.56. Further variants will buy hundredths, so the next
   move is to swap a leg for a new dataset (sentiment, systerm) rather
   than sweep more skew tenors.

Note on the r22 skew_360 → skew_180 → skew_90 sweep: the r22 leg probe
memory already recorded skew_180 (1.02) and skew_90 (0.96) as the two
strongest standalone skew legs. The reason `r21_t1060` and `r24_term_skew_rev`
both used skew_360 anyway is that they were designed before r22 ran. Today's
r25 round is the first to combine the r22 probe findings with the r24
returns-reversal recipe, and it produced two new GOOD alphas on the first
two candidates tried.

**Breakthrough (round 29, 2026-09-09): the analyst + option8 blend breaks the
SELF_CORRELATION ceiling while keeping EXCELLENT.**

After rounds 26–28 confirmed that:
- No analyst4 field other than `anl4_ebit_value` +
  `anl4_fs_detail_estimates_advanced_af_nd_netprofit_mean` reaches EXCELLENT
  (ebitda, fcf, cfo, bvps, grossincome all produce AVERAGE or INFERIOR).
- Parameter variations on the ebit+np recipe (decay 8→20, truncation
  0.01→0.05, lookback 60→180, industry instead of subindustry neutralization,
  2× weighting on either leg) all produce EXCELLENT but self_corr stays in
  0.85–0.92 — structurally stuck above the 0.7 limit against `2rOn70lb`.
- model16 rank-derivative factors are all INFERIOR (negative fitness, turnover
  0.5% < 1% limit) — too static to be useful.

The winning recipe blends the analyst signal (3 parts) with one option8
skew_180 leg (1 part):

```
ey = ts_backfill(anl4_ebit_value, 40) / cap
np = ts_backfill(anl4_fs_detail_estimates_advanced_af_nd_netprofit_mean, 40) / cap
s = ey + np
a = group_zscore(winsorize(ts_mean(s - ts_mean(s, 120), 20), std=3.0), subindustry)
k = ts_mean(implied_volatility_mean_skew_180, 20)
b = group_zscore(winsorize(k, std=3.0), industry)
group_zscore(winsorize(a + a + a + b, std=3.0), industry)
```

Settings: decay=12, truncation=0.01, neutralization=NONE, nan_handling=ON,
USA / TOP3000 / delay=1.

Result — **`vRkN3qzd` (r29_ebitnp_opt8skew_3to1), EXCELLENT, 8/8 PASS,
self-correlation=0.6857** (first unsubmitted EXCELLENT+8/8 PASS alpha in the
account):

| metric | value |
| --- | --- |
| sharpe | 2.27 |
| fitness | 2.25 |
| turnover | 4.8% |
| returns | 12.3% |
| drawdown | 4.4% |
| test_sharpe | 1.34 (59% OOS retention) |
| self_correlation | **0.6857** |

The skew_180 leg decorrelates just enough (0.6857 < 0.7) while the 3:1
weighting keeps the analyst signal dominant enough for EXCELLENT fitness.
The parallel blend using the IV-term-structure leg (`a+a+a+b` where b is
`-ts_mean(implied_volatility_mean_10/_60, 10)`) produced `3q9pZbZO`,
EXCELLENT fitness 2.29 but self_corr 0.8015 — the IV-term leg does not
decorrelate as effectively as the skew_180 leg.

**Key takeaway for future searches:** the analyst family's self-correlation
ceiling (~0.85 against 2rOn70lb) can be broken by adding a small (1/4 weight)
option8 skew_180 component. The skew_180 leg is the most effective decorrelator
among the option8 fields tested. The blend ratio 3:1 (analyst:option8) is the
sweet spot — enough analyst to keep EXCELLENT, enough option8 to push
self_corr below 0.7. The IV-term-structure leg is less effective at
decorrelation (self_corr 0.8015 at the same ratio).

**Round 30 (2026-09-09): opening `delay` and `pasteurization` for tuning does
NOT rescue a correlation-blocked factor — re-parameterizing the same factor is
not a new factor.** The user explicitly instructed (2026-09-09): even with
DELAY and PASTEURIZATION now adjustable, a previously-found factor does **not**
become usable just by swapping one of these parameters; if self-correlation
fails repeatedly you must change the **combination/recipe (the legs)**, not the
construction settings. Round 30 measured this three ways and confirmed it:

- **`pasteurization=OFF` boosts fitness but is still the same alpha for
  correlation.** Pure analyst recipe (the `2rOn70lb` shape) with pasteur=OFF:
  SPECTACULAR fitness **2.80** (vs 2.69 ON) but self_corr **0.9908**. The
  `vRkN3qzd` 3:1 analyst:skew_180 recipe with pasteur=OFF: EXCELLENT fitness
  2.32 (vs 2.25 ON) but self_corr **0.9921**. Round 30c then ran **12
  expression variants under pasteur=OFF** (skew_90/_180/_360, weights 2:1–4:1,
  IV-term leg, +returns-reversal leg, decay 8/12/16, truncation 0.01/0.02,
  industry/subindustry): every one graded EXCELLENT (fit 2.25–2.38) and every
  one whose check resolved showed self_corr **0.98–0.99**. Pasteurization
  changes the tradable-universe cleaning, not the underlying signal, so BRAIN
  still sees a near-duplicate. Treat pasteur=OFF as a fitness dial only.
- **`delay=0` does not carry the EXCELLENT family at all.** Analyst fields are
  *unknown variables* at delay=0 (the entire analyst family cannot run there),
  and option8 at delay=0 scores lower (best fit 1.49 AVERAGE vs 1.56 GOOD at
  delay=1). Full detail in [[wqbrain-account-limits]].
- **Ratio sweeps toward the analyst leg raise, not lower, correlation.**
  Round 30d (pasteur=ON) pushed the skew_180 blend far toward the analyst side:
  the 10:1 analyst:skew variant (`RRV7d8Xb`, SPECTACULAR fit 2.53) measures
  self_corr **0.9275**; the 5:1 variant (`1YxwJnVk`, SPECTACULAR fit 2.63) is
  also analyst-dominated. The 3:1 blend (`vRkN3qzd`) was the sweet spot because
  it carries *more* decorrelating option8 weight, not less. Redecorating the
  same ebit+np + skew_180 skeleton (j236RJ9j + a tiny skew leg at 5:1/10:1)
  stays in the analyst family and stays correlated. `j236RJ9j` re-checked at
  **0.7044** after `vRkN3qzd` was submitted — essentially unchanged.

**Actionable rule:** once two or three variants of a recipe fail
SELF_CORRELATION, stop sweeping settings/ratios on it — that buys hundredths of
fitness and does not move correlation. Change the **data**: swap a leg for a
different dataset or a different signal family so the holdings genuinely differ
(the round-29 breakthrough was exactly this: 3 parts analyst + 1 part option8
skew_180). Parameter openness (delay, pasteurization) is a way to *retune* a
fresh recipe, not a way to *resubmit* an already-blocked one.

Status note: `vRkN3qzd` is now **ALREADY_SUBMITTED** (in the account as of
2026-09-09), so its 0.6857 correlation is a settled submission, not an
available alpha. When choosing the next search target, look for a recipe whose
legs differ from BOTH `2rOn70lb` (SPECTACULAR analyst) and the
already-submitted `vRkN3qzd` (analyst+skew_180).

**Round 31 (2026-09-09): second submittable EXCELLENT found by changing the
DECORRELATOR DATA, not the settings — and the winning decorrelator has NO
option8 at all.** Applying the round-30 rule ("change the legs, don't re-tune"),
the analyst base `a` (ebit/cap + netprofit/cap, subindustry zscore, 120d/20d)
was blended with legs from datasets *other than* option8 skew, so the result
differs from both `2rOn70lb` (pure analyst) and the submitted `vRkN3qzd`
(analyst + option8 skew_180). Measured self-correlation per decorrelator leg
(analyst:leg weight in parentheses), all delay=1 / decay=12 / trunc=0.01 /
neutralization=NONE / pasteur=ON / nanHandling=ON:

| decorrelator leg(s) | dataset | fitness | self_corr | verdict |
| --- | --- | --- | --- | --- |
| skew_90 (3:1) | option8 | 2.25 | **0.9924** | FAIL — same option8 family as submitted vRkN3qzd |
| skew_360 (3:1) | option8 | 2.26 | **0.9881** | FAIL — same reason |
| systematic_risk 30/360 ratio (any weight/window) | model51 | 2.18–2.28 | 0.85 (when it resolved) | **CONCENTRATED_WEIGHT FAIL** — model51 ratio has sparse/tied values that pile into a few names under `group_zscore`; this leg is unusable in this blend (matches the round-22 discrete-field trap) |
| rel_ret_comp 10d (3:1) | pv13 | 2.23 | 0.8429 | FAIL |
| returns reversal −ts_mean(returns,5) (3:1) | pv1 | 2.11 | 0.7688 | FAIL |
| skew_180 + reversal (2:1:1) | option8+pv | 2.15 | 0.7900 | FAIL |
| **rel_ret_comp 10d + returns reversal 5d (2:1:1), NO option8** | pv13+pv | **2.10** | **0.6603** | **8/8 PASS — `9qVXrMle`** |

**The winning recipe (`9qVXrMle`, local name `r31_anl_relret_rev5_2to1to1`):**

```
ey = ts_backfill(anl4_ebit_value, 40) / cap
np = ts_backfill(anl4_fs_detail_estimates_advanced_af_nd_netprofit_mean, 40) / cap
s = ey + np
a = group_zscore(winsorize(ts_mean(s - ts_mean(s, 120), 20), std=3.0), subindustry)
c = group_zscore(winsorize(ts_mean(rel_ret_comp, 10), std=3.0), industry)      # pv13
d = group_zscore(winsorize(-ts_mean(returns, 5), std=3.0), industry)           # pv1 reversal
group_zscore(winsorize(a + a + c + d, std=3.0), industry)
```

Result: EXCELLENT, sharpe 2.08, fitness 2.10, turnover 11.6%, returns 12.7%,
drawdown 4.2%, balanced book (long 1418 / short 1397), **4/4 positive years
(worst 1.74)**, train sharpe 2.34 → test 0.71, self_corr **0.6603**.

Three lessons:

1. **Two moderate decorrelators compound.** rel_ret_comp alone (0.8429) and
   returns-reversal alone (0.7688) both fail at 3:1, but blending BOTH at 2:1:1
   drops self_corr to 0.6603 — the reduction is not linear; two independent
   non-analyst legs decorrelate the book more than either alone. This mirrors
   the round-29 finding that legs whose errors are independent are the ones
   that help.
2. **Changing skew TENOR does not decorrelate — changing DATASET does.**
   skew_90 (0.99) / skew_360 (0.99) are the same option8 field family as the
   already-submitted skew_180, so BRAIN still sees a near-duplicate. The leg
   that worked (`rel_ret_comp` pv13 + `returns` pv1) shares no dataset with
   either submitted alpha. When a field's dataset is already in the account at
   high fitness, swap the *dataset*, not just the window/tenor.
3. **The fundamental × relationship-graph × price-volume mix (no option8) is
   itself a strong, decorrelated theme.** `rel_ret_comp` (pv13 competitor
   return spillovers) and the −5d returns reversal are both positive-signal
   legs here (`rel_ret_comp` positive, reversal negated). The 11.6% turnover is
   higher than the 4.8% option8 blends but well within limits.

Caveat: test-year sharpe is 0.71 (30% of train 2.34) — same low-OOS-retention
ballpark as the SPECTACULAR `2rOn70lb` (27%). The 8/8 gate passes and 4/4 years
are positive, but treat the IS fitness as more trustworthy than the single
hold-out year. `9qVXrMle` is the second unsubmitted-but-submittable EXCELLENT
in the account (after `vRkN3qzd`, which is now ALREADY_SUBMITTED).

Also confirmed again this round: **delay=0 and pasteurization=OFF did not
rescue anything** (see round 30 above); the only lever that moved correlation
was the leg data. The six round-31 variants that failed only SELF_CORRELATION
(`Vk67PgMw`, `vRkjlglw`, `RRV7825e`, `blRjq9eK`, `rK5jlqqm`, `XgW7n0Wx`) were
tagged `SELF_CORRELATION_BLOCKED` so the selection pool skips them.

**Round 32–33 (2026-09-10): after 4 submissions the bar moved — the analyst
DETRENDED core has a correlation floor ~0.77; the breakthrough is a LEVEL
`ts_rank` analyst core. `9qVXrMle` and `MP1pYp0o` were submitted overnight, so
the account now contains four submitted alphas: `2rOn70lb` (pure detrended
analyst), `vRkN3qzd` (analyst + skew_180), `9qVXrMle` (analyst + pv13 + pv1),
`MP1pYp0o` (IV-term + skew_180 + reversal, GOOD). Findings:

- **Every detrended-analyst-core blend now floors at sc ~0.77.** Round 32
  (socialmedia12 sentiment/buzz + option9 pcr_oi_180 legs): 12/12 FAIL,
  sc 0.78–0.95. sentiment/pcr standalone PnL is too weak; the analyst core
  dominates. Round 33b (new legs model16 rank derivatives NEGATED, fnd6
  operating_income/cap, pv13 rel_num_all, news18): best 0.7746 — still over.
  Simply adding a leg to the exact `9qVXrMle` recipe measures **0.94** (it
  collides with the now-submitted 9qVXrMle itself).
- **Leg signs/data notes (r33a probes):** all 8 model16 `*_rank_derivative`
  fields are sign-NEGATIVE (flipped ~sh 0.6, turnover 0.7%, near-identical PnL
  — weak but stable diversifier); `operating_income/cap` positive sh 0.68;
  `rel_num_all` positive sh 0.70 but 0.7% turnover; `rel_ret_all` +0.61;
  news18 fields are EVENT inputs: `ts_mean`/`ts_backfill` reject them
  ("does not support event inputs") — must use `vec_avg(field)` first; even
  then `ts_mean(vec_avg(nws18_qep),5)` has ~zero standalone PnL (dead leg).
- **Non-analyst cores are decorrelated but capped at AVERAGE fitness ~1.47.**
  v2 (IV-term 10/60, 2×) + rel_ret_comp + pcr_oi_180 = `mLg6G5Kp`: 8/8 PASS,
  sc 0.5575, but fit 1.10. Adding skew_360 lifts fit to 1.47 (AVERAGE,
  sc 0.685, `npKPEk2l`); weighting sweeps (v3, k2/k3, c2, windows, skew_270,
  iv10/iv90, decay 8/12/16, trunc) never cross GOOD — round 33e, 11 AVERAGE.
  Data point: v2+c+pcr sc 0.56 vs v2+d(reversal)+pcr 0.81 — **rel_ret_comp is
  the unique-PnL decorrelator; the returns-reversal leg now correlates**
  (present in two submitted alphas). Avoid `-ts_mean(returns,5)` in new
  recipes.
- **BREAKTHROUGH — level `ts_rank` analyst core.** `A10GZ5ge`
  (`group_zscore(winsorize(ts_rank(s,120),std=3),subindustry)`, no detrending)
  was already GOOD fit 1.92 / sc 0.5909: the LEVEL rank has different PnL from
  the detrended `ts_mean(s - ts_mean(s,120),20)` core in all three analyst
  submitted alphas. Blending this core r (2–3×) + rel_ret_comp + pcr_oi_180
  produced today's wins (all delay=1/neutralization=NONE/pasteur=ON/nan=ON):

| alpha | recipe | grade | fit | sh | test sh | sc |
| --- | --- | --- | --- | --- | --- | --- |
| **`2rOjd9Vx`** (`r33g_r2cp_d7`) | r+r+c+p, decay=7 | **EXCELLENT** | 2.09 | 2.37 | 0.99 (train 2.65) | **0.6280** |
| `vRkOwYaG` (`r33f_r3cp`) | r+r+r+c+p, d16 | GOOD | 1.97 | 2.28 | 0.65 | 0.6658 |
| `0mRjOl18` (`r33f_r2cp`) | r+r+c+p, d16 | GOOD | 1.94 | 2.25 | 0.64 | 0.6648 |
| `wpY9g3rp` (`r33g_r2c2p_d14`) | r+r+c+c+p, d14 | GOOD | 1.83 | 2.17 | 0.63 | 0.5740 |
| `gJQWkgjO` (`r33f_r2up`) | r+r+rel_num_all+p, d16 | GOOD | 1.58 | 1.81 | 0.60 | 0.6729 |

  All 8/8 PASS (authoritative re-checks 2026-09-10), balanced books
  (~1320/1350), turnover 6–12%. Winning template:
  `r=gz(win(ts_rank(ts_backfill(anl4_ebit_value,40)/cap + ts_backfill(...netprofit_mean,40)/cap, 120),3),subindustry)`
  `c=gz(win(ts_mean(rel_ret_comp,10),3),industry)`,
  `p=gz(win(ts_mean(pcr_oi_180,20),3),industry)`,
  outer `gz(win(r+r+c+p,3),industry)`, **decay=7** gives EXCELLENT.
- **Correlation of the same recipe across its own variants:** the five winners
  share r+p and would flag one another AFTER one is submitted; the
  rel_num_all variant (`gJQWkgjO`) is the most distinct backup. Submit one per
  theme, then re-measure.
- **Operational trap: check-job ERROR after HTTP 429.** During 33f the check
  endpoint rate-limited; three alphas' SELF_CORRELATION checks got stuck
  permanently at `ERROR` (`A10Q997g` EXCELLENT fit 2.07 included — tagged
  CHECK_ERROR_STUCK). GET retries never clear it. Workaround that works:
  re-simulate the SAME expression with a changed setting (e.g. decay 7 vs 8)
  to get a fresh alpha id with a fresh check job — that produced the verified
  `2rOjd9Vx`. Also: a RUNNING sim can block the runner's resume path; set its
  `simulations.status` to FAILED in sqlite before restarting a batch.

Status 2026-09-10: five new unsubmitted 8/8 winners above; pre-existing
submittables `A10GZ5ge` (GOOD 0.5909) and `Vk627oWY` (GOOD 0.6987, knife-edge).
Detrended-analyst-core is CLOSED while the four submissions stand; future
rounds should start from the ts_rank level core or other non-analyst cores.

**Rounds 34–37 (2026-09-12): the user submitted `2rOjd9Vx`, which POISONED its
whole family — and the fitness/self-corr frontier of the ts_rank analyst family
is now exactly mapped.** Account submissions: five (`2rOn70lb`, `vRkN3qzd`,
`9qVXrMle`, `MP1pYp0o`, **`2rOjd9Vx`**).

1. **A submission kills its unsubmitted relatives.** After `2rOjd9Vx` went in,
   fresh checks on the 09-10 pool returned: `0mRjOl18` 0.9738, `vRkOwYaG`
   0.9664, `wpY9g3rp` 0.9421, `A10GZ5ge` 0.8682, `gJQWkgjO` 0.7318 — all FAIL.
   Only `Vk627oWY` (option8 family, no analyst core) survived at 0.6987.
   Stored `8/8 PASS` rows are CACHED snapshots: `2rOjd9Vx` kept returning its
   old 0.6280 for a while while sibling checks were stuck unresolved/recomputing.
   **Always recheck before submitting anything whose relatives exist in the
   account.**
2. **The measured sc ladder (all decay≈5–7, trunc 0.01, delay 1):**

| recipe (core r = gz(win(ts_rank(s,K),3),subindustry)) | fit | sc |
| --- | --- | --- |
| r2+r+c+p (k120) — the submitted 2rOjd9Vx shape | 2.09 | (submitted) |
| r3+r+c+p (k120) | 2.14 | 0.9891 |
| r3+r+c+o (p→o swap) | 2.03 | 0.8784 |
| r3+r+u+p (c→u swap) | 1.73 | 0.7526 |
| r3+r+u+o (k120, FULL swap) | 1.76 | ~0.727 |
| **r4+r+u+o k250** | **2.03–2.07** | **0.741–0.744 FAIL** |
| r4+r+u+o k350 | 1.94 | 0.7194 FAIL |
| **r3+r+u+o k250 d5/d7** | **1.86–1.90** | **0.685–0.687 PASS** |
| r4+r+u+o k500 | 1.78 | 0.6866 PASS |
| r2+r+u+o k120 | 1.48 | 0.6505 PASS |

   **The EXCELLENT+ point (fit ≥2.0 AND sc ≤0.7) is unreachable inside this
   family** — every +0.1 fitness from r-weight costs ~+0.04–0.06 sc, and the
   frontier crosses the 0.7 line between fit 1.94 and 2.03. u = `rel_num_all`
   (near-static, 0.7% turnover — mechanically uncorrelated PnL, the best
   diluter), o = `operating_income/cap`. Slower rank windows (k250→k500) shave
   ~0.03–0.05 sc AND raise fitness ~+0.1 (k250 > k120 > k60); d5 adds ~+0.04
   fit over d7 at neutral sc. c (rel_ret_comp) is the correlation carrier:
   keeping it costs +0.15 sc at r3.
3. **Today's submittable finds (all GOOD 8/8, one family — realistically ONE
   submission slot before the others die):** `WjP3AaPO` (r3+u+o
   k250 d5, fit 1.90 sc 0.6872, best), `KPOvGX7N` (same k250 d7, 1.86/0.6851),
   `E5vNlOqR` (r4+u+u+o k250 d7, 1.80/0.6593), `akLe7vew` (r4+u+o k500 d7,
   1.78/0.6866), `blRJQdkp` (r3+u+o+o k250 d7, 1.52/0.6582). Plus pre-existing
   `Vk627oWY` (GOOD 1.53, 0.6987).
4. **Dead ends measured today:** window/weight tweaks inside the r2+c+p family
   (c5/c20/p10/p2/k60/k250/sub — all 0.93–0.997); c as CORE `c+c+c+r+p`
   (INFERIOR 0.87, rel_ret_comp standalone too weak, 19.7% turnover); product
   form `r*c*p` (INFERIOR, sh −0.38); s2=ey+np+opinc as core (AVERAGE 1.42);
   socialmedia8 `snt_social_value` standalone ±0.37; news12 is VECTOR/event
   type (news18 trap again, skipped); fundamental2 `accrued_liabilities_total`
   Δ66d/cap negated (flipped +0.44, book 736/577) and
   `accumulated_depreciation_depletion_amortization_ppne/cap` negated (flipped
   +0.92, turnover 1.1%, book 1711/718 — a possible future slow leg, but low
   coverage/unbalanced-book risk).
5. **Dataset discovery works via the API**: `GET {base}/data-sets` and
   `/data-fields?dataset.id=...&orderBy=alphaCount&dir=DESC` (needs absolute
   URL through `client.get_json`). 14 datasets visible on this entitlement;
   unused: fundamental2 (766 fields), news12 (875, VECTOR-heavy), socialmedia8
   (4 fields), univ1 (universe selectors only).

**Next move for EXCELLENT+:** the analyst ts_rank family is saturated at its
frontier; a NEW fitness engine (standalone sh ≥ 1.0 from unmined data) or a
fundamentally different construction is required. Datasets probed on 09-12
(socialmedia8, fundamental2 accruals/D&A, news12) yielded nothing above 0.92
standalone and that one is unbalanced/static.

**Round 38–39 (2026-09-14): BREAKTHROUGH — mining UNUSED FIELDS inside already-used
datasets found two new fitness-engine legs (debt/cap sh~1.11, cogs/cap sh~1.07)
that broke the EXCELLENT+ barrier. 6 EXCELLENT + 2 GOOD 8/8 PASS in one round.**

Method: used the BRAIN API (`GET {base}/data-fields?dataset.id=...&orderBy=
alphaCount&dir=DESC`) to list top fields by usage inside fundamental6 (886 fields,
only 1 used), analyst4 (1324 fields, only 2 used), option9 (74 fields, only 1 used),
option8 (64 fields, only 4 used). Round 38 probed 12 standalone legs (positive
sign), all INFERIOR standalone but two came back strongly NEGATIVE because the sign
was flipped — the un-negated versions have sh > 1.0:

| leg | dataset | standalone sh (sign-corrected) | note |
| --- | --- | --- | --- |
| **debt/cap** (POSITIVE) | fnd6 | **~1.11** | negated probe returned -1.11; high leverage = higher return |
| **cogs/cap** (POSITIVE) | fnd6 | **~1.07** | negated probe returned -1.07; high cost-to-market = "value" |
| capex/cap (POSITIVE) | fnd6 | 0.99 | near threshold; high capex intensity |
| assets/cap (POSITIVE) | fnd6 | 0.86 | classic asset-based value |
| adj_net_income_avg/cap (POSITIVE) | anl4 | 0.86 | analyst adjusted NI |
| cashflow_op/cap (POSITIVE) | fnd6 | 0.80 | operating cashflow yield; best book balance |
| actual_eps_value_quarterly/cap (POSITIVE) | anl4 | 0.50 | weak |
| bookvalue_ps (POSITIVE) | fnd6 | 0.19 | dead |
| call_breakeven_180/close (POSITIVE) | opt9 | 0.07 | dead |
| forward_price_120/close (POSITIVE) | opt9 | 0.30 | weak |
| -implied_volatility_call_180 | opt8 | -0.01 | dead (negation wrong → +sign also weak) |
| -historical_volatility_120 | opt8 | 0.02 | dead |

All fundamental6 yield factors (X/cap) are POSITIVE sign. The two highest-alphaCount
unused fields in fundamental6 (assets 178k, cashflow_op 26k, debt 33k, capex 32k,
cogs 11k) are all worth probing — the field usage count is a valid signal-strength
prior. Round 38 also confirmed: fnd6 fields have ~1.3% turnover (very slow,
quarterly reporting) and unbalanced books standalone, but blending with the
continuous analyst core fixes the balance.

**Round 39 — the winning blends (all decay=7, trunc=0.01, k250, delay=1,
USA/TOP3000/NONE/ON/ON, P1Y):**

| alpha | recipe | grade | fit | sh | test | sc |
| --- | --- | --- | --- | --- | --- | --- |
| **`2rOxeAvJ`** (`r39_r3g2u`) | r3+g2+u (cogs×2+rel_num_all) | **EXCELLENT** | **2.26** | 2.16 | 1.37 | **0.6753** |
| **`WjPRJ53N`** (`r39_r4d2gu`) | r4+d2+g+u (debt×2+cogs+rel_num_all) | **EXCELLENT** | **2.25** | 2.09 | 0.89 | **0.6446** |
| **`LL9w3KJm`** (`r39_r3d2u`) | r3+d2+u (debt×2+rel_num_all) | **EXCELLENT** | **2.23** | 2.11 | 0.81 | **0.6536** |
| **`XgWxMZg1`** (`r39_r3d2gu`) | r3+d2+g+u (debt×2+cogs+rel_num_all) | **EXCELLENT** | **2.14** | 1.96 | 0.90 | **0.5887** |
| **`qMWpVVQj`** (`r39_r4d2g`) | r4+d2+g (debt×2+cogs) | **EXCELLENT** | **2.10** | 1.98 | 0.73 | **0.6155** |
| **`9qV32zMq`** (`r39_r3d2xu`) | r3+d2+x+u (debt×2+capex+rel_num_all) | **EXCELLENT** | **2.09** | 1.94 | 0.91 | **0.5791** |
| `gJQ5LavK` (`r39_r3d2g`) | r3+d2+g (debt×2+cogs) | GOOD | 2.00 | 1.85 | 0.72 | 0.5581 |
| `883ZXw9m` (`r39_r3d2g_k500`) | r3+d2+g k500 (debt×2+cogs, slow window) | GOOD | 1.87 | 1.77 | 0.91 | 0.5274 |

FAIL (EXCELLENT but sc > 0.7): `vRkbPGlr` (r4+d2+u, fit 2.32/sc 0.7076 — over
by 0.008), `d5O6vo1K` (r4+g2+u, fit 2.35/sc 0.7254).

**Key findings:**
- **cogs/cap is the strongest new decorrelator** — in the r3+g2+u blend, it
  pushes fitness to 2.26 (vs old r3+u+o 1.90) while keeping sc at 0.675 (vs
  old 0.687). The PnL pattern of cost-of-goods-sold relative to market cap
  is genuinely different from both the analyst estimate core AND the submitted
  alphas.
- **debt/cap is the strongest sc diluter** — blends with d2 (debt×2) consistently
  show sc 0.52–0.65, ~0.05–0.10 lower than equivalent cogs blends. Adding both
  d+g drops sc further (r3d2gu 0.589, r3d2g 0.558, r3d2g_k500 0.527).
- **The new fitness–sc frontier** (replacing the 09-12 map):
  - r3+g2+u k250 d7: fit 2.26/sc 0.675 ← **EXCELLENT+ sweet spot**
  - r4+d2+u k250 d7: fit 2.32/sc 0.708 ← over by 0.008
  - r4+g2+u k250 d7: fit 2.35/sc 0.725 ← over by 0.025
  - r3+d2+g k500 d7: fit 1.87/sc 0.527 ← safest (lowest sc)
- **debt/cap + cogs/cap together are the ultimate decorrelator pair** —
  they're from the same dataset (fnd6) but economically orthogonal (leverage
  vs cost efficiency), and their PnLs compound to give sc as low as 0.527.
- All 8 winners share r (analyst core) and most share d or g — submitting
  one will poison the family as before. The most distinct is `9qV32zMq`
  (uses capex instead of cogs) or `883ZXw9m` (k500 window).

**Today's submittable pool (14 total, 8 new + 6 from prior rounds):**
6 EXCELLENT (fit 2.09–2.26, sc 0.579–0.675) + 2 GOOD (fit 1.87–2.00, sc
0.527–0.558) from round 39; plus 4 GOOD from round 36–37 (fit 1.52–1.90,
sc 0.658–0.687) and `Vk627oWY` (GOOD 0.6987). Best pick for submission:
`2rOxeAvJ` (fit 2.26, test 1.37, sc 0.675) — highest fitness with strong OOS.

Three structural changes were required — round 14 tried the same option fields
with the analyst recipe and got **10/10 INFERIOR**:

1. **`decay` 8 → 16.** Option/sentiment data moves daily, so turnover landed at
   26-40%, far above the 0.125 floor in `fitness = sharpe × sqrt(|returns| /
   max(turnover, 0.125))`. Decay 16 got it to 6-11%.
2. **Standardize each leg before summing** — `group_zscore` every leg, then add,
   then `group_zscore` the sum. Different units otherwise let one leg dominate.
3. **Blend across datasets, not within one** — the fitness gain came from legs
   whose errors are independent, not from any correlation benefit (see the
   measurement below). A **fourth leg consistently hurt** (`pcr_oi_180` took
   1.30 → 1.21 and 1.38 → 1.25; `systerm` took 1.53 → 1.20); three is the sweet
   spot.

**What `SELF_CORRELATION` compares against: unknown, and my earlier hypothesis is
disproved.** I had guessed it compares only against *higher-graded* alphas. Round
23 refuted that: `r23_term_comp_sys_skew` (AVERAGE) shares three of its four legs
with `r21_t1060` (GOOD, higher-graded, fitness 1.53) and its `selfCorrelated`
recordset still came back **empty**. Under the hypothesis it should have been
compared and flagged.

The full measurement across all 39 alphas that pass 8/8: **38 have an empty
recordset** (`PASS` with `value=None`). The only one with a numeric correlation is
`2rOn70lb` (0.6996) — the analyst-family SPECTACULAR alpha.

Candidate explanations that remain consistent with the data, none confirmed: the
comparison set is *submitted* alphas only; or it is scoped per data theme/dataset;
or the recordset lists only correlations past some display threshold while the
PASS/FAIL verdict uses another rule. Do not build a plan on any of them.

What is nonetheless solid:

- The original finding stands but is **narrower than it looked**. The 0.72–1.0
  correlations were all inside the analyst-estimate family, and `npKzLemd` /
  `YP5Wj6Rl` at exactly 1.0 were literal duplicates of one another.
- Within the option8 × pv13 family, near-duplicate recipes do **not** flag each
  other, so `SELF_CORRELATION` is not the binding constraint there — **fitness
  is.** Do not keep switching data theme purely to dodge correlation; switch when
  the fitness landscape is saturated.
- This is a snapshot, not a property. It could change once alphas are actually
  submitted, so re-measure with `scripts/recheck_submissions.py` instead of
  assuming it holds.

**How to apply:** **at the current stage, do not treat correlation as a design
constraint** — instructed by the user on 2026-09-07, because very few alphas have
actually been submitted. This matches the measurement below, so pick themes and
legs for fitness and signal quality, not to dodge `SELF_CORRELATION`. Revisit once
submissions accumulate.

Beyond that: on a theme already represented at GOOD+ in the account, expect
`SELF_CORRELATION` to be the first check to fail — that is what the analyst family
did. On the option8 × pv13 family, do not expect it. Never treat a high grade as
progress toward a usable alpha. Refresh verdicts with
`python scripts/recheck_submissions.py --min-grade GOOD` — correlation drifts as
the account accumulates alphas, so a stored PASS is a snapshot, not a permanent
property. The check endpoint sometimes returns `SELF_CORRELATION` as `PENDING`
too; that counts as not passed.

Note on dedup: the search skips a candidate only when the same expression has
already run in the same **region/universe/delay** (`hashing.scope_hash`). Changing
a construction parameter alone (`decay`, `truncation`, `neutralization`,
`nanHandling`, `testPeriod`) still does not make a new alpha, so those cannot be
swept on a fixed expression — vary the expression too. The user approved this
granularity on 2026-09-07.

See [[wqbrain-leg-probe-findings]] for which other data families were measured and
rejected, [[wqbrain-account-limits]] for the closed delay/universe axes,
[[wqbrain-search-constraints]] for the standing search rules, and
[[wqbrain-api-verification-sources]] for the API contract.
