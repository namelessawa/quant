---
name: wqbrain-account-limits
description: Account-level constraints — TOP3000 beats every smaller USA universe by a wide margin; delay=0 became entitled on 2026-09-09 but analyst4 data is unavailable there and option8 scores worse at delay=0
metadata:
  type: project
---

Two axes measured live. Check these before designing a round, because a setting
can look available from the catalog yet behave very differently in a simulation.

**1. `delay=0`: entitlement opened on 2026-09-09, but it does not help the
proven recipes.** On 2026-09-07 delay=0 was rejected outright
(`400 {'settings': {'delay': ['Delay 0 is not available.']}}`). On 2026-09-09 the
account was entitled to it and simulations were accepted — but round 30 measured
two hard limitations:

- **analyst4 data is unavailable at delay=0.** Every expression using
  `anl4_ebit_value` (or any `anl4_*` field) fails at delay=0 with
  `Attempted to use unknown variable "anl4_ebit_value"`. The whole EXCELLENT
  analyst family cannot be run at delay=0. (`GET /data-fields` still lists the
  fields — catalog availability does not imply simulation availability, same
  lesson as the old entitlement error.)
- **option8 works at delay=0 but scores LOWER.** The proven option8×pv13 blend
  (`implied_volatility_mean_10/_60` + skew + returns-reversal) across 12 delay=0
  variants (decay 4/8/12/16, truncation 0.01/0.02, industry/subindustry,
  pasteur ON/OFF, different skew tenors and leg weights) produced best
  **fitness 1.49 (AVERAGE)** vs 1.56 (GOOD) at delay=1. Sharpes were
  1.54–1.82 vs 1.71–1.84, and low decay pushed turnover to ~20%. None reached
  GOOD. One variant looked attractive (test sharpe 2.07 vs IS 1.26) but that is
  the IS/test-divergence trap — do not trust it.

  Net: for both signal families, delay=1 is still the working axis. Treat delay=0
  as open-but-unproductive unless a future dataset is verified delay=0-native.

**2. TOP3000 is the right universe; smaller is much worse.** The same expression
(`r19_t1090_termdbl_skew360`, an IV term-structure × competitor-return blend) run
across universes at delay 1:

| universe | sharpe | fitness | returns | drawdown | test sharpe |
| --- | --- | --- | --- | --- | --- |
| TOP3000 | 1.71 | **1.42** | 8.7% | 6.7% | 1.41 |
| TOP1000 | 1.06 | 0.85 | 8.0% | 10.1% | 0.99 |
| TOPSP500 | 0.87 | 0.64 | 6.8% | 11.5% | **−0.12** |
| TOP200 | 0.78 | 0.65 | 8.6% | 13.2% | **−0.22** |

**Why:** the intuitive "smaller universe = less efficient = more alpha" is wrong
here. Returns barely move (6.8–8.7%) while sharpe collapses, and in TOPSP500 /
TOP200 the held-out year goes *negative* — the signal does not survive out of
sample at all. Fewer names also means a thinner book, so drawdown rises.

**How to apply:** do not spend quota sweeping universes again for this family of
signals — TOP3000 is settled, and delay=0 is open but unproductive for the known
families. Remaining gains come from signal composition (legs/datasets/recipe),
not from the settings scope. When a new data theme is tried, re-verify the
universe choice once, since a sparser signal could behave differently.

See [[wqbrain-selfcorrelation-bottleneck]] for the theme that these runs use and
[[wqbrain-api-verification-sources]] for the API contract.
