# Factor Registry / Alpha Memory Report

## 1. Total

- Total factors tracked: **427**
- Simulated (any terminal research state): **386**
- Passed gate: **63**
- Submitted: **0**
- Feature rows indexed: 427

## 2. Status Distribution

| Status | Count |
| --- | ---: |
| CORR_REJECTED | 63 |
| METRIC_REJECTED | 202 |
| PASSED | 63 |
| SIMULATED | 58 |
| SIMULATION_FAILED | 41 |

## 3. Submitted Alphas

_No submitted factors recorded. Run `import-submitted`.

## 4. Factor Families — Most and Least Tried

| Family | Trials | Passed | Submitted | Avg sharpe | Best fitness |
| --- | ---: | ---: | ---: | ---: | ---: |
| OPTION | 193 | 58 | 0 | 1.47 | 2.63 |
| ANALYST | 94 | 5 | 0 | 1.41 | 2.80 |
| PRICE_REVERSAL | 58 | 0 | 0 | 0.45 | 0.83 |
| MODEL | 44 | 0 | 0 | -0.09 | 0.98 |
| UNKNOWN | 18 | 0 | 0 | 0.04 | 0.34 |
| NEWS | 16 | 0 | 0 | 0.00 | 0.08 |
| FUNDAMENTAL | 3 | 0 | 0 | 0.42 | 0.50 |
| LIQUIDITY | 1 | 0 | 0 | -0.54 | -0.27 |

Underexplored (trials <= 5):
- FUNDAMENTAL (3 trial(s))
- LIQUIDITY (1 trial(s))

## 5. Most Common Structures (field templates)

- `group_zscore(winsorize(ts_mean(<FIELD>,<WINDOW>),std=3.0),<FIELD>)` — 28
- `assign(t,divide(<FIELD>,<FIELD>));assign(c,ts_mean(<FIELD>,<WINDOW>));assign(k,ts_mean(<FIELD>,<WINDOW>));assign(a,group_zscore(winsorize(negative(ts_mean(X,<WINDOW>)),std=3.0),<FIELD>));assign(b,group_zscore(winsorize(X,std=3.0),<FIELD>));assign(d,group_zscore(winsorize(X,std=3.0),<FIELD>));group_zscore(winsorize(add(add(add(X,X),X),X),std=3.0),<FIELD>)` — 20
- `assign(t,divide(<FIELD>,<FIELD>));assign(c,ts_mean(<FIELD>,<WINDOW>));assign(k,ts_mean(<FIELD>,<WINDOW>));assign(a,group_zscore(winsorize(negative(ts_mean(X,<WINDOW>)),std=3.0),<FIELD>));assign(b,group_zscore(winsorize(X,std=3.0),<FIELD>));assign(d,group_zscore(winsorize(X,std=3.0),<FIELD>));group_zscore(winsorize(add(add(X,X),X),std=3.0),<FIELD>)` — 19
- `assign(ey,divide(ts_backfill(<FIELD>,<WINDOW>),<FIELD>));assign(np,divide(ts_backfill(<FIELD>,<WINDOW>),<FIELD>));assign(s,add(X,X));group_zscore(winsorize(ts_mean(subtract(X,ts_mean(X,<WINDOW>)),<WINDOW>),std=3.0),<FIELD>)` — 19
- `assign(ey,divide(ts_backfill(<FIELD>,<WINDOW>),<FIELD>));assign(np,divide(ts_backfill(<FIELD>,<WINDOW>),<FIELD>));assign(s,add(X,X));assign(a,group_zscore(winsorize(ts_mean(subtract(X,ts_mean(X,<WINDOW>)),<WINDOW>),std=3.0),<FIELD>));assign(k,ts_mean(<FIELD>,<WINDOW>));assign(b,group_zscore(winsorize(X,std=3.0),<FIELD>));group_zscore(winsorize(add(add(add(X,X),X),X),std=3.0),<FIELD>)` — 18
- `negative(ts_rank(<FIELD>,<WINDOW>))` — 17
- `assign(ey,divide(ts_backfill(<FIELD>,<WINDOW>),<FIELD>));group_zscore(winsorize(ts_mean(subtract(X,ts_mean(X,<WINDOW>)),<WINDOW>),std=3.0),<FIELD>)` — 15
- `assign(ey,divide(ts_backfill(<FIELD>,<WINDOW>),<FIELD>));assign(raw,group_zscore(winsorize(ts_mean(subtract(X,ts_mean(X,<WINDOW>)),<WINDOW>),std=3.0),<FIELD>));assign(vol_state,ts_rank(ts_std_dev(<FIELD>,<WINDOW>),<WINDOW>));assign(liq_state,ts_rank(<FIELD>,<WINDOW>));trade_when(logical_and(gt(X,0.40),lt(X,0.90)),X,gt(X,0.97))` — 13
- `assign(t,divide(<FIELD>,<FIELD>));assign(k,ts_mean(<FIELD>,<WINDOW>));assign(r5,ts_mean(<FIELD>,<WINDOW>));assign(a,group_zscore(winsorize(negative(ts_mean(X,<WINDOW>)),std=3.0),<FIELD>));assign(d,group_zscore(winsorize(X,std=3.0),<FIELD>));assign(p,group_zscore(winsorize(negative(X),std=3.0),<FIELD>));group_zscore(winsorize(add(add(add(X,X),X),X),std=3.0),<FIELD>)` — 12
- `assign(t,divide(<FIELD>,<FIELD>));assign(v,group_zscore(winsorize(negative(ts_mean(X,<WINDOW>)),std=3.0),<FIELD>));assign(c,group_zscore(winsorize(ts_mean(<FIELD>,<WINDOW>),std=3.0),<FIELD>));assign(k,group_zscore(winsorize(ts_mean(<FIELD>,<WINDOW>),std=3.0),<FIELD>));assign(p,group_zscore(winsorize(ts_mean(<FIELD>,<WINDOW>),std=3.0),<FIELD>));group_zscore(winsorize(add(add(add(add(X,X),X),X),X),std=3.0),<FIELD>)` — 11

## 6. Most Used Data Fields

- `cap` — 147
- `anl4_ebit_value` — 120
- `rel_ret_comp` — 110
- `anl4_fs_detail_estimates_advanced_af_nd_netprofit_mean` — 109
- `implied_volatility_mean_10` — 92
- `returns` — 73
- `implied_volatility_mean_60` — 59
- `implied_volatility_mean_skew_360` — 56
- `close` — 54
- `implied_volatility_mean_skew_180` — 52
- `pcr_oi_180` — 47
- `implied_volatility_mean_90` — 32
- `implied_volatility_mean_30` — 21
- `volume` — 17
- `implied_volatility_mean_360` — 17

## 7. Correlation Clusters and Representatives

Threshold 0.70; 427 cluster(s), 0 with more than one member.

## 8. Saturated and Successful Combinations

Saturated (>=10 trials, <=10% submission rate):
- `ANALYST|DIVIDE|LIQUIDITY` — 144 trials, 0 submitted, avg sharpe 1.76
- `OPTION|DIVIDE|OPTION` — 123 trials, 0 submitted, avg sharpe 1.40
- `MODEL|DIVIDE|MODEL` — 17 trials, 0 submitted, avg sharpe 1.40

Successful (at least one submission):
- _none_

## 9. Recent Rejections

### Duplicate / low-novelty blocks
- _none_

### Correlation rejections
- id=418 `ey = ts_backfill(anl4_ebit_value, 40) / cap; np = ts_backfill(anl4_fs_detail_est` — SELF_CORRELATION_BLOCKED
- id=417 `ey = ts_backfill(anl4_ebit_value, 40) / cap; np = ts_backfill(anl4_fs_detail_est` — SELF_CORRELATION_BLOCKED
- id=416 `ey = ts_backfill(anl4_ebit_value, 40) / cap; np = ts_backfill(anl4_fs_detail_est` — SELF_CORRELATION_BLOCKED
- id=408 `t = implied_volatility_mean_10 / implied_volatility_mean_60; v = group_zscore(wi` — SELF_CORRELATION_BLOCKED
- id=406 `t = implied_volatility_mean_10 / implied_volatility_mean_60; v = group_zscore(wi` — SELF_CORRELATION_BLOCKED
- id=398 `t = implied_volatility_mean_10 / implied_volatility_mean_60; v = group_zscore(wi` — SELF_CORRELATION_BLOCKED
- id=397 `t = implied_volatility_mean_10 / implied_volatility_mean_60; v = group_zscore(wi` — SELF_CORRELATION_BLOCKED
- id=389 `t = implied_volatility_mean_10 / implied_volatility_mean_60; v = group_zscore(wi` — SELF_CORRELATION_BLOCKED
- id=385 `t = implied_volatility_mean_10 / implied_volatility_mean_60; v = group_zscore(wi` — SELF_CORRELATION_BLOCKED
- id=380 `ey = ts_backfill(anl4_ebit_value, 40) / cap; np = ts_backfill(anl4_fs_detail_est` — SELF_CORRELATION_BLOCKED

## 10. Research Coverage

### Underexplored datasets (trials <= 3)
- _none (or no field catalog loaded)_

### Underexplored operator structures (family templates <= 5)
- `add(negative(rank(ts_delta(PRICE,<WINDOW>))),rank(ts_std_dev(PRICE,<WINDOW>)))` (1 trial(s))
- `assign(a,ts_backfill(FUNDAMENTAL,<WINDOW>));assign(g,divide(ts_delta(a,<WINDOW>),a));group_zscore(winsorize(negative(g),std=3.0),GROUP)` (1 trial(s))
- `assign(b,OTHER);group_zscore(winsorize(ts_mean(subtract(b,ts_mean(b,<WINDOW>)),<WINDOW>),std=3.0),GROUP)` (1 trial(s))
- `assign(b,divide(ts_mean(OTHER,<WINDOW>),ts_mean(OTHER,<WINDOW>)));group_zscore(winsorize(negative(b),std=3.0),GROUP)` (1 trial(s))
- `assign(bt,MODEL);group_zscore(winsorize(negative(ts_mean(bt,<WINDOW>)),std=3.0),GROUP)` (1 trial(s))
- `assign(c,divide(ts_backfill(ANALYST,<WINDOW>),LIQUIDITY));assign(d,divide(ts_backfill(ANALYST,<WINDOW>),LIQUIDITY));assign(s,subtract(c,d));group_zscore(winsorize(ts_mean(subtract(s,ts_mean(s,<WINDOW>)),<WINDOW>),std=3.0),GROUP)` (1 trial(s))
- `assign(c,group_zscore(winsorize(ts_mean(MODEL,<WINDOW>),std=3.0),GROUP));assign(d,group_zscore(winsorize(negative(ts_mean(PRICE,<WINDOW>)),std=3.0),GROUP));assign(m,group_zscore(winsorize(negative(ts_mean(MODEL,<WINDOW>)),std=3.0),GROUP));group_zscore(winsorize(add(add(add(add(c,c),d),d),m),std=3.0),GROUP)` (1 trial(s))
- `assign(c,group_zscore(winsorize(ts_mean(MODEL,<WINDOW>),std=3.0),GROUP));assign(d,group_zscore(winsorize(negative(ts_mean(PRICE,<WINDOW>)),std=3.0),GROUP));group_zscore(winsorize(add(add(add(c,c),d),d),std=3.0),GROUP)` (1 trial(s))
- `assign(c,ts_mean(MODEL,<WINDOW>));group_zscore(winsorize(negative(c),std=3.0),GROUP)` (1 trial(s))
- `assign(ey,divide(ts_backfill(ANALYST,<WINDOW>),LIQUIDITY));assign(e,divide(ts_backfill(ANALYST,<WINDOW>),LIQUIDITY));assign(s,add(ey,e));group_zscore(winsorize(ts_mean(subtract(s,ts_mean(s,<WINDOW>)),<WINDOW>),std=3.0),GROUP)` (1 trial(s))

### Saturated clusters: 0 (min size 3, avg internal corr >= 0.70)
- _none_

## 11. DO_NOT_REPEAT — Dead Ends

- _none recorded_

### Failed research directions (by failure category)
- **Weak standalone signal (sharpe below bar)** — 169 failure(s): OPTION×44, PRICE_REVERSAL×42, MODEL×30
- **Strong idea but redundant vs an existing alpha/cluster** — 63 failure(s): ANALYST×34, OPTION×29
- **Simulation itself errored/timed out** — 41 failure(s): OPTION×15, NEWS×12, PRICE_REVERSAL×10
- **One-sided book (degenerate long/short profile)** — 17 failure(s): MODEL×14, ANALYST×3
- **Weak risk-adjusted quality (fitness below bar)** — 12 failure(s): OPTION×6, PRICE_REVERSAL×5, ANALYST×1
- **Great in train, collapses out of sample** — 3 failure(s): OPTION×3
- **Turns over too fast (cost / capacity)** — 1 failure(s): PRICE_REVERSAL×1

## 12. High-Quality Redundant — Change Data, Not Parameters

- id=284 sharpe=2.34 fitness=2.80 margin=-0.341 `ey = ts_backfill(anl4_ebit_value, 40) / cap; np = ts_backfill(anl4_fs_detail_est`
- id=314 sharpe=2.24 fitness=2.53 margin=-0.277 `ey = ts_backfill(anl4_ebit_value, 40) / cap; np = ts_backfill(anl4_fs_detail_est`
- id=299 sharpe=2.34 fitness=2.35 margin=-0.340 `ey = ts_backfill(anl4_ebit_value, 40) / cap; np = ts_backfill(anl4_fs_detail_est`
- id=283 sharpe=2.34 fitness=2.32 margin=-0.342 `ey = ts_backfill(anl4_ebit_value, 40) / cap; np = ts_backfill(anl4_fs_detail_est`
- id=306 sharpe=2.34 fitness=2.32 margin=-0.341 `ey = ts_backfill(anl4_ebit_value, 40) / cap; np = ts_backfill(anl4_fs_detail_est`

_Rule: these signals are real. Keep the idea, switch legs/dataset/family — do NOT retune window/decay/truncation._
