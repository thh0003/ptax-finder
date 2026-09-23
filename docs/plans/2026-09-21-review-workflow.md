# Parcel Structure Change Detection — Plan C: Review Workflow

Created: 2026-09-21
Author: tholmes4005@gmail.com
Agent: Claude Code
Status: PENDING
Approved: No
Iterations: 0
Worktree: No
Type: Feature

> Draft outline written during Plan A planning so the PRD's review, map, audit, and export scope is accounted for. Finalize by running `/spec docs/plans/2026-09-21-review-workflow.md` after Plan B is VERIFIED; the planning phase will re-ground each task in the code Plans A and B produced.

## Summary

**Goal:** A reviewer can work a run's candidate queue — side-by-side and swipe imagery per parcel, confirm/dismiss with notes, full who/when history — browse and review any parcel from a map with parcel overlay and status highlighting, and export the run's results (filterable to confirmed) as CSV for the county's CAMA system (PRD Flows 5, 6, 7).

Depends on Plan A (tenancy, parcels, frontend shell) and Plan B (`runs`, `run_parcels`, tile endpoint, per-parcel previews).

## Out of Scope

- Automatic valuation or reassessment notices — PRD out of scope.
- Live CAMA/ArcGIS sync — CSV export is the integration.
- Editing or deleting review history — the audit trail is append-only.

## Approach

**Chosen:** Review state lives in an append-only `review_events` table (parcel, run, actor, action, note, timestamp) from which the current status is derived; the queue, map, and export all read the same run-scoped view.
**Why:** An append-only log gives the defensibility the PRD requires without a separate audit table, at the cost of a derived-status query that must be indexed per run.

## Progress Tracking

- [ ] Task 1: `review_events` schema, status derivation view, migration
- [ ] Task 2: Review API — queue listing (sort/filter by score, status, parcel ref), parcel detail with history, confirm/dismiss/note actions, review of non-candidate parcels
- [ ] Task 3: CSV export endpoint (parcel ref, status, score, reviewer, review date, note; `?status=confirmed` filter)
- [ ] Task 4: Frontend review queue page with before/after thumbnails and keyboard next/previous
- [ ] Task 5: Frontend parcel review panel — side-by-side and swipe/toggle imagery, boundary overlay, actions, notes, history
- [ ] Task 6: Frontend map view (MapLibre GL) — year imagery basemap via Plan B tiles, parcel overlay from `/api/parcels?bbox=`, status highlighting, click-to-review
- [ ] Task 7: Export UI and run results summary

## Implementation Tasks

_To be written at finalization following the Plan A task format. E2E scenarios will cover: working the queue end to end, history attribution across two reviewers, confirming a non-candidate parcel from the map, and a CSV export whose rows match the reviewed state._
