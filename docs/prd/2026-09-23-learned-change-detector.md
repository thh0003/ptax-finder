# Learned Change Detector

Created: 2026-09-23
Author: tholmes4005@gmail.com
Agent: Claude Code
Category: Feature
Status: Prototype passed its decision gate (2026-09-23); production integration is a follow-up plan
Research: Light — existing tools and datasets checked, none benchmarked (see Technical Context)

## Problem Statement

The product claim is that a run over a county returns a short, trustworthy review queue: parcels that gained a structure between two aerial captures. The classical detector cannot deliver that, and this is measured rather than argued.

Plan D (`docs/plans/2026-09-22-detector-accuracy.md`) judged it against 300 Hennepin County parcels labelled by eye. At the shipped defaults it flags 30% of parcels at **28.7% precision**, against a **17.95%** base rate of visible improvement. No threshold or minimum structure size reaches the target of **≤2% flagged at ≥60% precision**. Worse, precision falls to **exactly zero** once the queue is small enough to work: its highest-scoring parcels are all wrong. A ranker that reads no imagery at all — inverse parcel size — scores higher average precision (0.2578 against 0.2236).

Drawing the detector's markup on the evaluation set showed why. Of its 20 highest-scoring parcels, ranks 1–13 are all false positives, and **none of the top ten scoring structures is a roof**. They are bare ground: graded house lots, a dried pond, a harvested field stripe, a dirt track, bare yard patches. The classical cue keeps bright, smooth, compact, recently changed ground, and a graded lot is exactly that — so its largest "structures" are lots prepared for building but not yet built on. That is the failure a new detector has to fix.

## Core User Flows

### Flow 1: Assessor works a run's queue
1. An admin starts a run comparing two imagery years over the county, as today.
2. The run flags a small share of parcels, ranked by confidence that a new structure appeared.
3. The reviewer works the queue from the top. Most flagged parcels at the top show a real new structure, not a graded lot or a changed field.
4. For each one the parcel viewer shows *where* the detector found the change, as it does today.

### Flow 2: Team evaluates a candidate detector
1. The team runs `ptax-eval score` with the candidate detector against the visual labels.
2. The report shows precision, recall, flag rate and average precision at the county base rate, beside the classical detector and the no-imagery baseline.
3. `ptax-eval chips --ranked --markup` shows the candidate's top-ranked parcels with its markup, so the team can see *what* it gets wrong, not only how often.
4. The candidate ships only if it beats both comparisons on the same set.

## Scope

### In Scope
- A learned detector that ranks parcels by confidence that a structure appeared between a base and a target capture.
- Behind the existing `Detector` seam (`compare(base, target, …) -> ChangeResult`), so runs, storage, the API and the viewer work unchanged.
- Emitting the two markup masks the viewer and run storage already depend on: `new_builtup_mask` and `structure_mask` (the region the score rests on).
- Scoring by the size of the new structure, respecting the run's `min_new_area_m2` parameter (default 37.2 m² = 400 ft²).
- Evaluation against the 300 visual labels through `ptax-eval`, with the classical detector and the no-imagery baseline reported alongside.
- A way to choose the detector per run, so both can run side by side while the new one is proven.
- Running at county scale within the existing run job: resumable, committed in batches, hundreds of thousands of parcels.

### Explicitly Out of Scope
- **Changes to the review workflow** — confirm/dismiss, the queue UI, notes and history are Plan C's.
- **New imagery sources** — the detector works on the NAIP and uploaded imagery the platform already ingests.
- **Re-scoring completed runs** — a run keeps the score and markup it recorded; a new detector applies to new runs only.
- **Classifying the kind of improvement** (house, garage, pool). The first target is "a new structure appeared"; type can follow.
- **Retuning the classical detector.** Plan D recorded its ceiling and stopped tuning by decision. It stays as the comparison and the fallback.

## Success Criteria

Measured with `ptax-eval score` on `backend/eval/nw-hennepin-2010-2021.json` against `visual-labels-nw-hennepin-2010-2021.json`, reweighted to the 17.95% county base rate.

| Bar | Criterion |
|---|---|
| **Must beat** | Average precision above both the classical detector (0.2236) and the no-imagery baseline (0.2578) |
| **Must beat** | Precision in the top 1% of the ranking above 0 — the classical detector's is exactly zero |
| **Target** | The product operating point: **≤2% flagged at ≥60% precision** |
| **Named failure** | Graded lots with no building no longer fill the top of the ranking, checked with `ptax-eval chips --ranked --markup` |

The *Must beat* rows decide whether the detector ships. The *Target* is the product goal. If it is missed, the measured best is recorded, exactly as Plan D recorded the classical ceiling.

## Technical Context

- **Relevant architecture:** `ClassicalDetector.compare` in `backend/src/ptax/detection/detector.py` implements the `Detector` protocol and returns a `ChangeResult` (`score`, `candidate`, `indicators`, `new_builtup_mask`, `structure_mask`). `backend/src/ptax/detection/run.py` reads each parcel's two years onto a common projected grid (`comparison_resolution`, coarsest year, 0.5 m floor), applies one radiometric fit per run, calls `compare`, and stores the masks as polygons on `run_parcels`. The module docstring already anticipates "a learned detector … behind the same `compare` signature".
- **Evaluation:** `backend/src/ptax/eval/` scores any detector over 300 parcels drawn from three strata by assessor build year, reweighted to the county population. The visual labels are the ground truth. The assessor's build year only sets each parcel's sampling weight; it disagreed with the imagery on 31 of 300 parcels.
- **Existing tools, not benchmarked here:**
  - *Per-year building segmentation, then compare the years.* Segment buildings in each capture, and take new building area as the change. A model trained to see buildings should already separate roofs from bare soil, and it needs no change labels. Candidates: torchgeo's U-Net and FarSeg segmentation models, fine-tuned from its NAIP-pretrained backbones; and samgeo, which applies Meta's Segment Anything to aerial imagery.
  - *Supervised change detection.* torchgeo includes change-detection models (BTC, ChangeStar, ChangeViT, FC-Siamese). They need labelled before/after pairs, which this project does not have at training scale.
  - *Weak labels.* Microsoft's open US building footprints (~130M polygons, ODbL licence) are also on Planetary Computer as `ms-buildings`. That is the same credential-free source `ptax-eval` already uses. They are a single snapshot, from source imagery dated roughly 2014–2021, so they could label "a building is here", not "a building appeared".
- **Constraints:**
  - The 300 visual labels are the only trusted truth. **They are for evaluation, not training.** Training on them would leave nothing independent to judge by.
  - Workers run on ECS Fargate, and Fargate offers no GPUs. A GPU would mean running ECS on EC2 GPU instances or ECS Managed Instances. Inference must be affordable on CPU at county scale, or the plan must change the compute stack and say so.
  - A county run is tens of thousands to a few hundred thousand parcels, committed in batches of 200. Per-parcel cost is paid at that scale; the classical detector measured about 70 ms per parcel on the 25-parcel fixture layer, mostly spent reading the imagery. No real-imagery figure has been measured.
  - Imagery arrives at 0.3–1.0 m, sometimes RGB-only (uploads) and sometimes RGB+NIR (NAIP), and the two years may differ in resolution. The run already compares at the coarser year's resolution.
  - The labelled set covers one area: a fast-growing fringe of northwest Hennepin County. It cannot show whether a model works elsewhere.
- **Related work:** Plan D holds the evaluation harness and the measured ceiling. The Parcel Change Viewer (`docs/plans/2026-09-22-parcel-change-viewer.md`) consumes the two markup masks. Plan C (`docs/plans/2026-09-21-review-workflow.md`) is the queue this detector feeds, and its value is capped by this detector's precision.

## Key Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Ground truth | The visual labels, not the assessor's build year | Decided in Plan D. The app compares two images and looks for improvements; the build year records something else and disagreed on 31 of 300 parcels. |
| Evaluation set | Kept out of training | The only trusted labels. Training on them would make every score self-graded. |
| Parcel size | Not a factor in scoring or evaluation | Decided in Plan D. The product must find improvements on a parcel of any size; a size-matched evaluation set was explicitly rejected. |
| What the score measures | Size of the new structure, not share of the parcel | Decided in Plan D. A building is the same size wherever it stands; share-of-parcel scoring was beaten by the no-imagery baseline. |
| Integration point | The existing `Detector` seam, masks included | Runs, storage, API and viewer stay unchanged, and a run keeps showing what it recorded. |
| Rollout | Selectable per run, beside the classical detector | Lets the new detector be compared on real runs before it becomes the default, and keeps a fallback. |

## Open Questions

1. **Which approach first?** The leading hypothesis is per-year building segmentation compared across years, because it needs no change labels and targets the graded-lot failure directly. It is unverified on 0.6–1.0 m NAIP. The plan should prototype it against the visual labels before committing to infrastructure.
2. **Where does training data come from,** if fine-tuning is needed? Options: building footprints as weak per-year labels (with the imagery-date mismatch that brings), a small separately labelled training set, or none, if a pretrained model is good enough.
3. **CPU or GPU?** CPU inference on Fargate at county scale may be too slow or too costly. The answer decides whether the compute stack changes.
4. **Does it generalise?** One labelled area cannot show that. Is a second evaluation area — another county or another part of Hennepin — a condition for shipping, or a follow-up?
5. **How many labels are enough?** 99 of the 300 labelled parcels improved, and precision in the top 1% of a county-weighted ranking rests on very few of them. How much uncertainty is acceptable before a result counts as beating the baselines?

## Answers from the prototype (2026-09-23)

Plan `docs/plans/2026-09-23-learned-change-detector.md`; evidence in `backend/eval/README.md`, "Learned detector — segmenter-v1 and the decision gate".

1. **Which approach first?** Per-year building segmentation, compared across years. `segmenter-v1` scored AP **0.588 (95% CI 0.422–0.772)** against 0.224 (classical) and 0.258 (no imagery), precision @ top 1% 1.00, and 19 of its top 20 parcels are real improvements with every top-20 structure on a roof. Graded lots no longer fill the top of the ranking. **Must beat: passed.**
2. **Training data?** Microsoft building footprints as per-pixel labels, over four neighbourhoods built out before 2000, in four NAIP years (2010–2021) — no hand labelling, and the visual labels were never used for training or tuning.
3. **CPU or GPU?** CPU. About 74 ms per parcel on one core for two inferences; a 200 000-parcel run is ~4 CPU-hours of inference on the dev machine, more on Fargate vCPUs. No GPU needed; Fargate timing is still to be measured.
4. **Does it generalise?** Unknown — still one labelled area. The integration plan should add a second before the segmenter becomes the default.
5. **How many labels are enough?** A stratified bootstrap interval is now printed on every score. The AP margin is clear of both baselines at its lower bound. The **≤2% / ≥60% target** is met on the point estimate (1.7% flagged at precision 1.00) but rests on 14 sampled parcels; one extra false positive drops it to 0.67, so that operating point needs more labels before it is claimed.

