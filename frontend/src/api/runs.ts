import { apiFetch } from "./client";

export type RunStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export type RunYear = { id: string; year: number; source: string; provider: string | null };

/** The detector a comparison run is started with (the only one). */
export type Detector = "classical";
/**
 * Every detector a stored run may carry: `vision` scores structure inventories, and
 * `segmentation` was a detector since removed, kept so past runs still display.
 */
export type StoredDetector = Detector | "segmentation" | "vision";

/** A base-vs-target comparison, or a single-year structure inventory. */
export type RunKind = "change" | "inventory";

export const STRUCTURE_KINDS = ["house", "garage", "shed", "pool", "other"] as const;
export type StructureKind = (typeof STRUCTURE_KINDS)[number];

export type Run = {
  id: string;
  kind: RunKind;
  status: RunStatus;
  /** An inventory's one year. */
  base_year: RunYear;
  /** Null for an inventory. */
  target_year: RunYear | null;
  threshold: number;
  min_new_area_m2: number;
  detector: StoredDetector;
  /** The model the run was given (vision, or the removed segmenter); null for classical. */
  model_name: string | null;
  parcels_total: number;
  parcels_processed: number;
  candidates: number;
  parcels_skipped: number;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

export const RUN_ACTIVE: RunStatus[] = ["queued", "running"];
// Measured best for the classical detector: see backend/eval/README.md.
export const DEFAULT_THRESHOLD = 0.3;
// Smallest contiguous new structure worth reporting: 400 sq ft.
export const DEFAULT_MIN_NEW_AREA_M2 = 37.2;
export const DEFAULT_DETECTOR: Detector = "classical";

const STORED_DETECTOR_NAMES: Record<StoredDetector, string> = {
  classical: "Classical",
  segmentation: "Segmenter (removed)",
  vision: "Vision model",
};

/** "Classical", or "Vision model · qwen3-vl": which detector, and exactly which model. */
export function detectorLabel(run: Pick<Run, "detector" | "model_name">): string {
  const name = STORED_DETECTOR_NAMES[run.detector];
  return run.model_name ? `${name} · ${run.model_name}` : name;
}

/** "Inventory · 2015 · qwen3-vl" for an inventory; "2015 → 2019" for a comparison. */
export function runLabel(run: Pick<Run, "kind" | "base_year" | "target_year" | "model_name">): string {
  if (run.kind === "inventory" || run.target_year === null) {
    return ["Inventory", String(run.base_year.year), run.model_name].filter(Boolean).join(" · ");
  }
  return `${run.base_year.year} → ${run.target_year.year}`;
}

export type Indicator = { key: string; label: string; unit: string; percent?: boolean };

/**
 * The measurements behind a score, in the order a reviewer reads them. Each detector
 * records different ones -- the segmenter has no vegetation measure -- so a row it never
 * produces would only ever read "—".
 */
const INDICATORS: Record<StoredDetector, Indicator[]> = {
  classical: [
    { key: "structure_m2", label: "Largest new structure", unit: "m²" },
    { key: "new_builtup_m2", label: "New built-up area", unit: "m²" },
    { key: "veg_loss_m2", label: "Vegetation loss", unit: "m²" },
    { key: "resolution_m", label: "Compared at", unit: "m/px" },
  ],
  // Kept for runs scored before the segmenter was removed.
  segmentation: [
    { key: "structure_m2", label: "Largest new structure", unit: "m²" },
    { key: "new_builtup_m2", label: "New building area", unit: "m²" },
    { key: "base_building_frac", label: "Base-year building share", unit: "%", percent: true },
    { key: "resolution_m", label: "Compared at", unit: "m/px" },
  ],
  // The structures themselves are listed separately (see `inventoryOf`).
  vision: [{ key: "resolution_m", label: "Read at", unit: "m/px" }],
};

export function indicatorsFor(detector: StoredDetector): Indicator[] {
  return INDICATORS[detector];
}

export function listRuns(): Promise<Run[]> {
  return apiFetch<Run[]>("/api/runs");
}

export function createRun(input: {
  base_year_id: string;
  target_year_id: string;
  threshold?: number;
  min_new_area_m2?: number;
  detector?: Detector;
}): Promise<Run> {
  return apiFetch<Run>("/api/runs", { method: "POST", body: JSON.stringify(input) });
}

export function createInventory(yearId: string): Promise<Run> {
  return apiFetch<Run>("/api/runs/inventory", {
    method: "POST",
    body: JSON.stringify({ year_id: yearId }),
  });
}

export function cancelRun(id: string): Promise<Run> {
  return apiFetch<Run>(`/api/runs/${id}/cancel`, { method: "POST" });
}

// --- A run's per-parcel results ----------------------------------------------------------

export type RunParcel = {
  parcel_id: string;
  parcel_ref: string;
  score: number | null;
  candidate: boolean;
  skipped_reason: string | null;
  /** Whether this run recorded where it found the change. False also for older runs. */
  has_markup: boolean;
  /** An inventory parcel's one-line summary and the kinds found; null for a comparison. */
  summary?: string | null;
  kinds?: string[] | null;
};

export type RunParcelDetail = RunParcel & {
  /** Numbers for a comparison; an inventory also stores its structures and summary. */
  indicators: Record<string, unknown> | null;
  /** The parcel's stored county fields. */
  parcel_attributes: Record<string, string | number | boolean | null> | null;
};

export type InventoryStructure = { kind: string; confidence: number };

/** The structures and summary an inventory stored, keeping only well-formed entries. */
export function inventoryOf(indicators: Record<string, unknown> | null): {
  structures: InventoryStructure[];
  summary: string | null;
} {
  const raw = Array.isArray(indicators?.structures) ? indicators.structures : [];
  const structures = raw.flatMap((item: unknown) => {
    if (typeof item !== "object" || item === null) return [];
    const { kind, confidence } = item as Record<string, unknown>;
    return typeof kind === "string" && typeof confidence === "number" ? [{ kind, confidence }] : [];
  });
  const summary = typeof indicators?.summary === "string" ? indicators.summary : null;
  return { structures, summary };
}

export type RunParcelPage = { items: RunParcel[]; total: number };

/**
 * The single source of the render parameters for a parcel's images.
 *
 * All three panes must cover the same ground, and they are three separate requests to
 * two different endpoints. The backend's shared extent function only guarantees that for
 * identical `size` and `buffer`, so building the URLs from one object here is the other
 * half of the guarantee — three independently constructed query strings drift the moment
 * someone edits one. `outline` is on for all three because the PRD wants the parcel
 * boundary on every pane, and the preview endpoint defaults it off.
 */
export const PARCEL_VIEW = { size: 512, buffer: 0.25, outline: true } as const;

function viewQuery(): string {
  return new URLSearchParams({
    size: String(PARCEL_VIEW.size),
    buffer: String(PARCEL_VIEW.buffer),
    outline: String(PARCEL_VIEW.outline),
  }).toString();
}

export function parcelImageUrls(input: {
  runId: string;
  parcelId: string;
  baseYearId: string;
  /** Null for an inventory, whose overlay draws on its one (base) year. */
  targetYearId: string | null;
}): { base: string; target: string | null; overlay: string } {
  const query = viewQuery();
  const preview = (yearId: string) =>
    `/api/imagery/years/${yearId}/parcels/${input.parcelId}/preview.png?${query}`;
  return {
    base: preview(input.baseYearId),
    target: input.targetYearId === null ? null : preview(input.targetYearId),
    overlay: `/api/runs/${input.runId}/parcels/${input.parcelId}/overlay.png?${query}`,
  };
}

export function listRunParcels(
  runId: string,
  params: { limit?: number; offset?: number; candidate?: boolean; structure?: StructureKind } = {},
): Promise<RunParcelPage> {
  const query = new URLSearchParams();
  if (params.limit !== undefined) query.set("limit", String(params.limit));
  if (params.offset !== undefined) query.set("offset", String(params.offset));
  if (params.candidate !== undefined) query.set("candidate", String(params.candidate));
  if (params.structure !== undefined) query.set("structure", params.structure);
  const suffix = query.toString();
  return apiFetch<RunParcelPage>(`/api/runs/${runId}/parcels${suffix ? `?${suffix}` : ""}`);
}

export function getRunParcel(runId: string, parcelId: string): Promise<RunParcelDetail> {
  return apiFetch<RunParcelDetail>(`/api/runs/${runId}/parcels/${parcelId}`);
}

/**
 * Append a freshly-loaded page to the rows already shown.
 *
 * Skips any parcel already present rather than concatenating blindly: rows are ordered by
 * score and a run being re-read while a reviewer scrolls can shift them, which would
 * otherwise show one parcel twice and silently push another off the end of the list.
 */
export function appendPage(current: RunParcel[], page: RunParcelPage): RunParcel[] {
  const seen = new Set(current.map((row) => row.parcel_id));
  return [...current, ...page.items.filter((row) => !seen.has(row.parcel_id))];
}
