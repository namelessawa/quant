---
name: wqbrain-leg-probe-findings
description: Measured sign and strength of every signal leg probed outside the winning recipe (round 22), plus three recurring traps — inverted signs, unbalanced books from discrete fields, and IS/test divergence
metadata:
  type: project
---

Round 22 (2026-09-07) probed 11 candidate legs standalone, one per simulation,
to find a new data theme after the option8 × pv13 recipe saturated. **All 11 came
back INFERIOR.** Measured with the proven structure (`group_zscore(winsorize(...))`
on `industry`, `ts_mean` smoothed, `decay=16`, `truncation=0.02`,
`neutralization=NONE`, `nanHandling=ON`, TOP3000/delay 1):

| leg | dataset | sharpe | fitness | turnover | returns | drawdown | test |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `systematic_risk_last_30_days / _360_days`, negated | model51 | **0.91** | 0.73 | 6.8% | 8.0% | 10.9% | 0.58 |
| `skew_360 − skew_30`, as written | option8 | **−0.90** | −0.68 | 7.2% | −7.1% | 54.6% | −1.51 |
| `scl12_sentiment`, negated | socialmedia12 | 0.58 | 0.28 | 7.5% | 2.9% | 6.3% | 0.48 |
| `buzz20 / buzz120`, negated | socialmedia12 | 0.38 | 0.12 | 9.2% | 1.3% | 6.5% | **1.26** |
| `parkinson_30 / historical_30` | option8 | 0.37 | 0.14 | 6.6% | 1.8% | 9.2% | 0.75 |
| `implied_vol_30 / historical_vol_30` (VRP) | option8 | 0.31 | 0.12 | 7.0% | 1.8% | 8.5% | 0.21 |
| `unsystematic_risk_30 / _360` | model51 | 0.25 | 0.09 | 6.6% | 1.8% | 11.0% | −0.43 |
| `rel_ret_part` | pv13 | −0.17 | −0.03 | 15.8% | −0.6% | 7.3% | 0.03 |
| `beta_last_30_days_spy` | model51 | −0.14 | −0.06 | 5.6% | −2.7% | 42.4% | −0.64 |
| `historical_vol_10 / _120` | option8 | −0.13 | −0.03 | 8.7% | −0.7% | 11.8% | −0.27 |
| `historical_volatility_30` level | option8 | 0.05 | 0.01 | 4.0% | 1.1% | **79.6%** | −0.24 |

**Verdict:** no new family beats the incumbent. The best new leg (0.91) is well
below what the IV term-structure and `rel_ret_comp` legs contribute inside a blend
(1.5+). So the move is to graft `systerm` into the proven recipe, not to abandon
it. `model51`, `socialmedia12` and the realized-volatility side of `option8` are
all weak; only `systematic_risk_last_30_days / _360_days` is worth keeping.

**Why this is worth recording:** each of these numbers cost a live simulation, and
the signs in particular cannot be reasoned out — three separate fields turned out
to need the *opposite* of the economically motivated sign.

**How to apply — three recurring traps:**

1. **Probe legs standalone before blending, and read the sign off the measured
   sharpe, not off the story.** Inverted so far: `implied_volatility_mean_skew_30`
   (−0.92 as theorized, +0.96 flipped), `pv13_custretsig_retsig` (−0.69),
   `skew_360 − skew_30` (−0.90). A wrong sign is not a weak signal — it is a
   strong signal pointed the wrong way, and blending it in silently cancels a good
   leg.
2. **Discrete or near-static fields produce badly unbalanced books under
   `group_zscore`.** `scl12_sentiment` gave 2042 long / 975 short;
   `pv13_com_page_rank` gave 774 / 2030 with 0.6% turnover. Many tied values pile
   names onto one side. Check `long_count`/`short_count` on every probe, not just
   the sharpe.
3. **IS/test divergence in either direction means "do not trust it".** `buzz`
   measured train 0.19 → test 1.26; `forward_price_90 / close` measured IS 0.01 →
   test 1.62. A held-out year that beats the in-sample window by that much is not
   a free lunch, it is an unstable signal.

**Method note that worked:** measurement rounds run through
`scripts/run_backtest.py --input` so every candidate completes; hunting rounds run
through `scripts/search_alpha.py` which stops at the first hit and therefore loses
the rest of the comparison. Round 21 stopped at 10 of 11 for exactly that reason.
Pick the runner by what the round is *for*.

**Round 33a addendum (2026-09-10), 18 standalone legs post-4-submissions:**

| leg | dataset | standalone result |
| --- | --- | --- |
| all 8 `*_rank_derivative` fields, **negated** | model16 | ~sh 0.6, turnover **0.7%**, PnLs near-identical to one another — weak but very slow diversifiers; as-written signs are flipped |
| `operating_income/cap` (ts_backfill 40) | fundamental6 | positive, sh 0.68 — best new fundamental leg, still below incumbent r/c |
| `rel_num_all` | pv13 | positive, sh 0.70, turnover 0.7% — strong but static; worked in blend `gJQWkgjO` |
| `rel_ret_all` | pv13 | positive sh 0.61, to 10% |
| other pv13 `rel_*` fields | pv13 | weak |
| `nws18_qep` / `nws18_ssc` | news18 | **EVENT inputs** — `ts_mean`/`ts_backfill` error; correct wrap `ts_mean(vec_avg(x),5)`; still ~0 PnL → dead family |
| `-ts_mean(returns,5)` reversal | pv1 | strong standalone but now in TWO submitted alphas → raises sc in any blend (0.56 → 0.81 in v2+c+p vs v2+d+p); do not use |

See [[wqbrain-selfcorrelation-bottleneck]] for the recipe these legs feed into and
[[wqbrain-account-limits]] for the closed delay/universe axes.
