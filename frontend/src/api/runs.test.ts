import { describe, expect, it, vi } from "vitest";

vi.mock("./client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./client")>()),
  apiFetch: vi.fn().mockResolvedValue({}),
}));

import { apiFetch } from "./client";
import {
  DEFAULT_DETECTOR,
  createRun,
  PARCEL_VIEW,
  appendPage,
  detectorLabel,
  indicatorsFor,
  parcelImageUrls,
} from "./runs";
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

describe("the detector a run used", () => {
  it("defaults new runs to the segmenter", () => {
    expect(DEFAULT_DETECTOR).toBe("segmentation");
  });

  it("names the segmenter together with the exact model it ran", () => {
    expect(detectorLabel({ detector: "segmentation", model_name: "segmenter-v1" })).toBe(
      "Segmenter · segmenter-v1",
    );
    expect(detectorLabel({ detector: "classical", model_name: null })).toBe("Classical");
  });

  it("shows only the measurements the run's detector actually produces", () => {
    const keys = (d: "classical" | "segmentation") => indicatorsFor(d).map((i) => i.key);

    expect(keys("classical")).toContain("veg_loss_m2");
    expect(keys("segmentation")).not.toContain("veg_loss_m2");
    expect(keys("segmentation")).toEqual(
      expect.arrayContaining(["structure_m2", "new_builtup_m2", "base_building_frac", "resolution_m"]),
    );
  });
});

describe("starting a run", () => {
  it("sends the chosen detector in the request body", async () => {
    await createRun({ base_year_id: "b", target_year_id: "t", detector: "classical" });

    const [path, init] = vi.mocked(apiFetch).mock.calls.at(-1)!;
    expect(path).toBe("/api/runs");
    expect(JSON.parse((init as RequestInit).body as string)).toMatchObject({
      base_year_id: "b",
      target_year_id: "t",
      detector: "classical",
    });
  });
});
