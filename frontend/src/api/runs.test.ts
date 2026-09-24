import { describe, expect, it, vi } from "vitest";

vi.mock("./client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./client")>()),
  apiFetch: vi.fn().mockResolvedValue({}),
}));

import { apiFetch } from "./client";
import {
  DEFAULT_DETECTOR,
  createInventory,
  createRun,
  PARCEL_VIEW,
  appendPage,
  detectorLabel,
  indicatorsFor,
  inventoryOf,
  listRunParcels,
  parcelImageUrls,
  runLabel,
} from "./runs";
import type { Run, RunParcel, RunParcelPage } from "./runs";

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
    const query = [urls.base, urls.target!, urls.overlay].map(
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
    for (const url of [urls.base, urls.target!, urls.overlay]) {
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
  it("starts new comparison runs with the classical detector", () => {
    expect(DEFAULT_DETECTOR).toBe("classical");
  });

  it("still names runs scored by the removed segmenter, with the model they ran", () => {
    expect(detectorLabel({ detector: "segmentation", model_name: "segmenter-v1" })).toBe(
      "Segmenter (removed) · segmenter-v1",
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

describe("structure inventories", () => {
  const year = (y: number) => ({ id: `y${y}`, year: y, source: "arcgis", provider: null });
  const run = (over: Partial<Run>): Run => ({
    id: "r1",
    kind: "change",
    status: "running",
    base_year: year(2015),
    target_year: year(2019),
    threshold: 0.3,
    min_new_area_m2: 37.2,
    detector: "segmentation",
    model_name: "segmenter-v1",
    parcels_total: 10,
    parcels_processed: 0,
    candidates: 0,
    parcels_skipped: 0,
    error: null,
    created_at: "2026-09-23T00:00:00Z",
    started_at: null,
    finished_at: null,
    ...over,
  });

  it("labels an inventory by its one year and model, and a comparison by its two years", () => {
    const inventory = run({
      kind: "inventory",
      target_year: null,
      detector: "vision",
      model_name: "qwen3-vl",
    });
    expect(runLabel(inventory)).toBe("Inventory · 2015 · qwen3-vl");
    expect(runLabel(run({}))).toBe("2015 → 2019");
    expect(detectorLabel(inventory)).toBe("Vision model · qwen3-vl");
  });

  it("starts an inventory on one imagery year", async () => {
    await createInventory("y2015");

    const [path, init] = vi.mocked(apiFetch).mock.calls.at(-1)!;
    expect(path).toBe("/api/runs/inventory");
    expect((init as RequestInit).method).toBe("POST");
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({ year_id: "y2015" });
  });

  it("filters a run's parcels by structure kind", async () => {
    await listRunParcels("r1", { limit: 25, structure: "garage" });

    const [path] = vi.mocked(apiFetch).mock.calls.at(-1)!;
    expect(new URL(path as string, "http://x").searchParams.get("structure")).toBe("garage");
  });

  it("gives an inventory parcel one image and its markup, with no target year", () => {
    const urls = parcelImageUrls({
      runId: "r1",
      parcelId: "p1",
      baseYearId: "y2015",
      targetYearId: null,
    });
    expect(urls.base).toContain("/api/imagery/years/y2015/parcels/p1/preview.png");
    expect(urls.target).toBeNull();
    expect(urls.overlay).toContain("/api/runs/r1/parcels/p1/overlay.png");
  });

  it("reads the structures and summary a parcel's inventory stored, ignoring junk", () => {
    const read = inventoryOf({
      structures: [
        { kind: "house", confidence: 0.9, box: [1, 2, 3, 4] },
        { kind: 7, confidence: "high" },
      ],
      summary: "A house and a shed.",
      counts: { house: 1 },
    });
    expect(read).toEqual({
      structures: [{ kind: "house", confidence: 0.9 }],
      summary: "A house and a shed.",
    });
    expect(inventoryOf(null)).toEqual({ structures: [], summary: null });
  });

  it("has only the one measurement an inventory records", () => {
    expect(indicatorsFor("vision").map((i) => i.key)).toEqual(["resolution_m"]);
  });
});
