---
name: wqbrain-search-constraints
description: Standing rules the user set for BRAIN alpha search runs in D:\wq — vary the alpha each round, mandatory 1-year test period, 8-check submission gate, xlsx recording, no duplicates
metadata:
  type: feedback
---

Standing constraints for every alpha search run in this project, set by the user
on 2026-09-06 after the first EXCELLENT alpha was found:

1. **Vary the alpha after every find — but not necessarily the dataset.** Once a
   target grade is hit, the next round must not repeat it verbatim. Acceptable
   variation includes other fields from a dataset already used, or new
   combinations of fields already used. Switching to a wholly different dataset
   also counts. What is forbidden is submitting something 原封不动 (unchanged).

   *Reaffirmed by the user on 2026-09-07:* **trying different combinations of the
   same factor is explicitly allowed** — varying legs, weights, windows and tenors
   around one recipe is expected work, not duplication. The user's stated filter
   is the *output*, not the *input*: "最后 check submission 阶段 8 pass 才保存". So
   explore broadly and let the 8-PASS gate decide what counts; do not narrow the
   candidate space to avoid near-duplicates.

   *Delegated by the user on 2026-09-07:* **when I judge a theme to be
   saturated, I may switch themes on my own** — no need to ask first. Saturation
   in practice looked like this: a round of 11 variants of one recipe all landed
   in a narrow fitness band (1.32–1.53), every knob available in that recipe
   (leg weights, smoothing windows, tenor pairs, grouping level, construction
   parameters) had already been swept, and the two remaining scope axes were
   closed (delay 0 not entitled, smaller universes strictly worse). Further
   variants were buying hundredths. That is the point to change data theme rather
   than keep tuning.

   Switching theme means keeping the **structure** that works and replacing the
   **data**: each leg `group_zscore`-standardized on its own, `ts_mean` smoothed,
   summed with weights, `group_zscore`d again, `decay=16`, `truncation=0.02`,
   `neutralization=NONE`, `nanHandling=ON`, `industry` grouping. Probe new legs
   standalone first to learn their sign and strength, then blend the best two or
   three — that two-step is what took the option/pv13 theme from 10/10 INFERIOR
   to GOOD in three rounds.
2. **Always keep a 1-year test period** — `settings.testPeriod = "P1Y"`. This is
   mandatory, not optional. It splits the IS window into train/test, so the alpha
   response gains `train` and `test` blocks and `stage` may read `OS`.
3. **Other parameters may be adjusted moderately** (decay, truncation,
   neutralization, windows) — the user expects tuning, but not a wholesale
   redesign of the winning recipe.
4. **Record the alpha in the xlsx ledger** (`data/experiments.xlsx`), one row per
   simulation, including the expression, the alpha id/link, dataset, fields,
   parameters and results.
5. **Never produce two identical alphas.** No exact-duplicate submissions.
   *Granularity settled by the user on 2026-09-07:* "identical" means the same
   expression over the same **information set** — `region` + `universe` + `delay`.
   Running one expression across TOP3000/TOP1000/TOPSP500/TOP200 or delay 0/1
   produces genuinely different alphas and is **allowed** (that is what makes a
   controlled universe sweep possible). Changing only portfolio-construction
   settings — `decay`, `truncation`, `neutralization`, `nanHandling`,
   `testPeriod` — does **not** make a new alpha, because one expression run under
   two `truncation` values once produced effectively identical results (both
   fitness 2.07) and wasted quota. Practical consequence: construction parameters
   can no longer be swept on a fixed expression; vary the expression too.
   Implemented as `hashing.scope_hash()` / `ResultStore.simulated_scope_hashes()`.
6. **A candidate only counts after BRAIN's submission check passes.** For every
   factor, look at the test-stage data *and* run check-submission
   (`GET /alphas/{alpha_id}/check`), waiting for it to return. Save/accept the
   alpha **only when all 8 checks PASS**:
   `LOW_SHARPE`, `LOW_FITNESS`, `LOW_TURNOVER`, `HIGH_TURNOVER`,
   `CONCENTRATED_WEIGHT`, `LOW_SUB_UNIVERSE_SHARPE`, `SELF_CORRELATION`,
   `MATCHES_COMPETITION`.
   If any check FAILs, read the failure reason, then either fix the factor and
   retry or abandon it. A high grade alone is never sufficient evidence.

   This step cannot be skipped: `SELF_CORRELATION` reads `PENDING` in the plain
   `GET /alphas/{id}` payload and only resolves through the explicit check call.
   The check endpoint is **asynchronous** — it answers `200` with an empty
   `text/html` body plus a `Retry-After` header while still computing, so it must
   be polled until it returns JSON.
7. **Mix data categories inside one alpha.** Instructed by the user on
   2026-09-07: later rounds should combine several different kinds of data in a
   single simulation — 量价结合 (price-volume combined with other categories) was
   the example given.

   This does **not** contradict the recorded finding that pure price-volume is a
   dead end (57 consecutive INFERIOR, fitness ceiling 0.83). That verdict was for
   *standalone* pv signals under the old structure — bare `rank(ts_delta(close,5))`,
   decay 0, no `group_zscore`, no smoothing, which produced 94.7% turnover. As one
   standardized, smoothed leg inside a multi-category blend, pv has never been
   tried. The structure lesson from rounds 14→16 applies: the same field can be
   useless or strong depending on how it is prepared.

**Why:** the user is building a portfolio of genuinely distinct, submittable
alphas across BRAIN's data catalog, not squeezing one dataset dry. Duplicate or
near-duplicate alphas waste simulation quota and would fail `SELF_CORRELATION`.
The test period guards against overfitting to the in-sample window. The 8-check
gate is the user's definition of "actually usable": grade follows fitness alone
and ignores submittability, so alphas graded GOOD have been observed carrying
2-3 failing checks and a long-only degenerate book.

**How to apply:** before launching a round, check which datasets and fields have
already been used (the ledger's `datasets` / `fields` columns are the record) and
pick unused fields or new combinations. Confirm `testPeriod` is present in the
settings that actually go over the wire — per-row CSV settings and `--setting`
overrides can silently drop it. Deduplicate on expression, not just
expression+settings: the same expression under two truncation values produced
effectively identical results (both fitness 2.07) and wasted a simulation. Treat
a candidate as a hit only when its grade meets the target **and** all 8
submission checks PASS **and** its test-period sharpe has not collapsed;
otherwise record the failing check reasons and either revise the expression or
drop it.

See [[wqbrain-api-verification-sources]] for the API contract these runs depend
on, and [[wqbrain-selfcorrelation-bottleneck]] for why constraint 1 now has to
mean a different **data theme**, not just different fields: as of 2026-09-07 only
one of the ten stored GOOD-or-better alphas actually clears `SELF_CORRELATION`.
