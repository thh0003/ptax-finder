# Parcel Change Viewer

Created: 2026-09-22
Author: tholmes4005@gmail.com
Agent: Claude Code
Category: Feature
Status: Draft
Research: None

## Problem Statement

A reviewer deciding whether a parcel gained a taxable structure currently gets a number and two pictures. The score says *how much* the detector thinks changed; nothing says *where*. The reviewer has to re-find the change by eye across two aerial images of the same field, and a confirmation that may later be challenged rests on that unaided comparison.

The same gap blocks the team. Plan B's AWS verification flagged 23 of 25 parcels, and the cause was only diagnosable by dumping per-parcel indicators out of the database and reasoning about them. Whether the detector marked a roof or the whole parcel is invisible in the product. Plan D is about to change the detector repeatedly, and every iteration needs the same question answered visually: what did it mark, and is it really there?

One view answers both: the base-year image, the target-year image, the target-year image with the detected new structures drawn on it, and the score that decision rests on.

## Core User Flows

### Flow 1: Reviewer inspects a flagged parcel
1. Reviewer opens a parcel from a run (from the queue, the map, or a direct link).
2. Sees the base-year image and the target-year image side by side, each clipped to the parcel with surrounding context and the parcel boundary drawn.
3. Sees a third view: the target-year image with the detected new built-up area marked on it.
4. Sees the parcel's change score, whether it was flagged as a candidate, and the measurements behind that score (new built-up area, vegetation loss, the resolution the two years were compared at).
5. Toggles the markup off and on over the target image to check the marked area against the bare imagery.
6. Moves to the next parcel.

### Flow 2: Team evaluates detector output over a sample
1. Team member opens parcels from a run, highest score first.
2. For each, compares the marked area against what is visibly present in the two years.
3. Recognises systematic failure at a glance — for example markup covering an entire parcel rather than a structure — and carries that into detector work.

### Flow 3: Parcel the run could not score
1. Reviewer opens a parcel the run skipped.
2. Sees why it was skipped (no imagery in the base year, none in the target year, or partial coverage) in place of a blank or misleading image, along with whichever year's imagery does exist.

## Scope

### In Scope
- A per-parcel view showing three images for a given run: base year, target year, and target year with new structures marked.
- All three clipped to the parcel with surrounding context and the parcel boundary drawn, showing the same ground area so they can be compared directly.
- A markup layer showing the new built-up area the run detected, visually distinct from the parcel boundary.
- A control to show and hide the markup over the target image.
- The parcel's score, candidate flag, and the stored per-parcel measurements displayed with the images.
- Skipped parcels show the reason and whatever imagery exists, rather than empty panes.
- The view is addressable per (run, parcel) so a specific parcel can be linked to and returned to.
- Runs record the per-parcel detected area at scoring time, so the markup a reviewer sees always corresponds to the score and decision recorded for that run.

### Explicitly Out of Scope
- **Confirm/dismiss, notes, and review history** — these belong to Plan C's review workflow and compose into this same panel; this PRD specifies what the reviewer *sees*, Plan C specifies what they *do*.
- **The review queue and map** — Plan C. This view is opened from them.
- **Editing or correcting the markup** — v1 shows what the detector produced. Hand-drawn corrections would be a labelling tool; Plan D's evaluation set may need one, and it should be specified there against its own requirements rather than smuggled in here.
- **A separate vegetation-loss overlay** — vegetation loss contributes to the score and is shown as a number, but "new structure" is the product claim and the marked area should mean exactly that.
- **Re-running detection from this view** — the view reads a completed run; starting runs stays on the Runs page.
- **Comparing more than two years at once** — a run is a pair of years by definition.
- **Retrofitting markup onto runs completed before this ships** — those runs did not record the detected area; they keep their scores and show no markup.

## Technical Context

- **Relevant architecture:** Per-parcel imagery is already served clipped to a parcel with an optional boundary outline by `GET /api/imagery/years/{year_id}/parcels/{parcel_id}/preview.png` (`backend/src/ptax/api/imagery.py`), which also returns the rendered extent in an `X-Bounds` header. Per-parcel run results live in `run_parcels` (`score`, `candidate`, `skipped_reason`, `indicators`), written in batches by `backend/src/ptax/detection/run.py`.
- **Existing code:** `ClassicalDetector.compare` in `backend/src/ptax/detection/detector.py` already computes the per-pixel new-built-up area it reports as `new_builtup_m2`, but returns only scalars — the mask itself is discarded today, so nothing currently persists where the change was found.
- **Constraints:**
  - A county run covers tens of thousands to a few hundred thousand parcels, and runs already commit per batch; anything recorded per parcel is recorded at that scale.
  - Detection happens on a common projected grid whose resolution is derived from both years (`comparison_resolution`), which is not the same grid as a display image rendered at a requested pixel size.
  - Imagery endpoints require the caller's bearer token, and the browser cannot attach headers to an `<img>` src; the frontend already fetches authenticated imagery as blobs (`frontend/src/api/uploads.ts`).
  - Runs are pinned to the parcel layer current when they started, and parcel identity across re-uploads is `parcel_ref`, not `parcels.id`.
- **Related work:** Plan C (`docs/plans/2026-09-21-review-workflow.md`) owns the review queue, parcel review panel, map and export; this view becomes that panel's imagery and score half. Plan D (`docs/plans/2026-09-22-detector-accuracy.md`) will change what the detector marks; recording the mask per run keeps completed runs' pictures consistent with their recorded scores while the detector changes underneath.

## Key Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Who the view serves | Reviewers and the team evaluating the detector, in one view | Both are asking the same question — what did it flag, and is it really there. A separate internal-only viewer would be duplicated work and would rot. |
| Where it lives | Becomes Plan C's parcel review panel rather than a second parcel view | Avoids two competing parcel views; Plan C's actions and history compose onto the same screen. |
| What the markup shows after the detector changes | The area the run recorded when it scored the parcel | A reassessment may be challenged; the picture a reviewer acted on must keep matching the score they acted on. Plan D will change detector output repeatedly, and a live-recomputed markup would silently disagree with older recorded decisions. |
| What is marked | New built-up area only | That is what "new structure" means in the product; vegetation loss is a supporting measurement and is shown as a number. |
| Markup presentation | A third view of the target image, with the markup toggleable | The request is to see the new image and a marked-up version of it; a toggle lets the reviewer check the marked area against bare imagery without leaving the parcel. |
| Pre-existing runs | Keep their scores, show no markup | Backfilling would mean re-running detection with today's code against runs scored by older code, producing pictures that disagree with recorded scores. |
