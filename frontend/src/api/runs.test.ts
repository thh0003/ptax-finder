import { describe, expect, it } from "vitest";

import { PARCEL_VIEW, appendPage, parcelImageUrls } from "./runs";
import type { RunParcel, RunParcelPage } from "./runs";

describe("parcel image URLs", () => {
  const urls = parcelImageUrls({
    runId: "r1",
    parcelId: "p1",
    baseYearId: "y2021",
    targetYearId: "y2023",
  });

  /**
   * The three panes are three separate HTTP calls. The backend's shared extent function
   * only guarantees the same ground for the same `size` and `buffer`, so if these URLs
   * are built independently the markup silently points at slightly the wrong place and
   * nothing errors. One parameter object is what prevents that.
   */
  it("gives all three images identical size, buffer and outline", () => {
    const query = [urls.base, urls.target, urls.overlay].map(
      (u) => new URL(u, "http://x").searchParams,
    );
    for (const key of ["size", "buffer", "outline"] as const) {
      const values = query.map((q) => q.get(key));
      expect(new Set(values).size, `${key} differs across the three images`).toBe(1);
      expect(values[0]).toBe(String(PARCEL_VIEW[key]));
    }
  });

  it("draws the parcel boundary on every pane, not just the overlay", () => {
    // `parcel_preview` defaults `outline` to false; the PRD wants it on all three.
    expect(PARCEL_VIEW.outline).toBe(true);
    for (const url of [urls.base, urls.target, urls.overlay]) {
      expect(new URL(url, "http://x").searchParams.get("outline")).toBe("true");
    }
  });

  it("points each pane at the right year and the overlay at the run", () => {
    expect(urls.base).toContain("/api/imagery/years/y2021/parcels/p1/preview.png");
    expect(urls.target).toContain("/api/imagery/years/y2023/parcels/p1/preview.png");
    expect(urls.overlay).toContain("/api/runs/r1/parcels/p1/overlay.png");
  });
});

describe("appending a page of a run's parcels", () => {
  const parcel = (ref: string): RunParcel => ({
    parcel_id: `id-${ref}`,
    parcel_ref: ref,
    score: 0.5,
    candidate: false,
    skipped_reason: null,
    has_markup: false,
  });
  const page = (refs: string[], total: number): RunParcelPage => ({
    items: refs.map(parcel),
    total,
  });

  it("concatenates a page onto what is already shown, in order", () => {
    const rows = appendPage([parcel("a"), parcel("b")], page(["c", "d"], 4));

    expect(rows.map((r) => r.parcel_ref)).toEqual(["a", "b", "c", "d"]);
  });

  it("drops a parcel the list already shows rather than repeating it", () => {
    // Scores shift if the run is re-read mid-scroll, so a page can overlap the last one.
    const rows = appendPage([parcel("a"), parcel("b")], page(["b", "c"], 3));

    expect(rows.map((r) => r.parcel_ref)).toEqual(["a", "b", "c"]);
  });

  it("leaves the list untouched for an empty page", () => {
    const current = [parcel("a")];

    expect(appendPage(current, page([], 1))).toEqual(current);
  });
});
