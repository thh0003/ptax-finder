# Baseline: `ClassicalDetector` before Plan D

Measured 2026-09-22 against the committed evaluation sets, with the detector exactly as
Plan B left it. `eval/out/` is gitignored, so these are the numbers Tasks 4-7 quote as
"before".

```bash
cd backend
uv run ptax-eval score eval/nw-hennepin-2010-2021.json   # threshold 0.3, min_new_area 40 m2
uv run ptax-eval score eval/nw-hennepin-2013-2017.json
```

## Headline

| | 2010 → 2021 (cross-resolution) | 2013 → 2017 (control, both 1.0 m) |
|---|---|---|
| scored / skipped | 300 / 0 | 258 / 0 |
| county base rate | 0.0599 | 0.0236 |
| **flag rate** (reweighted) | **0.9623** | 0.9334 |
| **precision** (reweighted) | **0.0442** | 0.0240 |
| recall | 0.7100 | 0.9483 |
| F1 | 0.0832 | 0.0468 |
| average precision | 0.0334 | 0.0189 |
| precision @ top 1% | 0.0000 | 0.0000 |
| precision @ top 5% | 0.0000 | 0.0087 |

**The failure reproduces, slightly harder than on AWS.** Plan B's deployed run flagged
23 of 25 parcels (92%); this measures a reweighted flag rate of **96.2%**, within the 15
point band the plan required before Task 4 could start.

**Precision is below the base rate.** At 0.0442 against a base rate of 0.0599, the
detector is *worse than flagging parcels at random* — and `precision @ top 1% = 0` means
the highest-scoring parcels contain no real construction at all. The score carries
negative information, not merely weak information.

## What is actually wrong

Medians per stratum, 2010 → 2021:

| stratum | n | `new_builtup_m2` | `veg_loss_m2` | base → target built-up | score |
|---|---|---|---|---|---|
| `positive` (built 2011-2021) | 100 | 182 | 65 | 0.677 → **0.569** | 0.621 |
| `negative_old` (built ≤ 2010) | 100 | **1468** | 1164 | 0.212 → 0.381 | **1.000** |
| `negative_future` (built > 2021) | 100 | 22 | 0 | **0.949 → 1.000** | 0.102 |

Two readings, and neither is a tuning problem:

1. **Bare ground classifies as built-up.** `negative_future` parcels are vacant in both
   captures — platted or farmed, no structure — and read as 95-100% built-up.
   `_classify` calls a pixel built-up when it is non-vegetated, bright and locally smooth,
   which is a description of bare soil, gravel and dry grass, not of a roof.
2. **Building a house *lowers* the built-up fraction.** Real construction runs
   0.677 → 0.569, because a finished house replaces graded bare soil with lawn. The cue
   therefore points the wrong way on exactly the event it is supposed to detect, which is
   why untouched established lots (score 1.000) outrank real new builds (0.621).

## Plan B's diagnosis does not survive

Plan B attributed a `target_builtup_frac` median of 0.937 against `base_builtup_frac`
0.366 — a gap of **+0.571** — to the 0.6 m target year being smoother once resampled onto
the 1.0 m grid, and Plan D's Task 4 was written to remove that bias.

| pair | native resolutions | measured median gap |
|---|---|---|
| 2010 → 2021 | 1.0 m → 0.6 m (differ) | **+0.015** |
| 2013 → 2017 | 1.0 m → 1.0 m (identical) | **+0.160** |

The cross-resolution pair shows almost no gap, and the **same-resolution control shows a
gap ten times larger**. A resolution mismatch cannot explain a bias that is worse when
there is no mismatch. Plan B's figure came from 25 synthetic-fixture parcels on one
quarter-quad; over 300 real parcels the effect is not there.

Recorded under Plan D's `## Deviations`: Task 6 (re-derive the classification) is promoted
ahead of Tasks 4 and 5, because tuning a resolution or radiometric correction against a
classifier that cannot tell a roof from a ploughed field would fit constants to an
artefact — the exact failure the plan's ordering rationale was meant to prevent.

## Score distribution

Over the 300 scored parcels: minimum 0.0000, median 0.7479, maximum 1.0000, with **27% of
parcels at or above 0.99** and 179 distinct values. The saturation Plan D's Task 7
describes is real and visible here: a quarter of the sample is pinned at the top of the
range, so `threshold` cannot separate them.
