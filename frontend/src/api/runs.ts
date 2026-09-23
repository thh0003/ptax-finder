import { apiFetch } from "./client";

export type RunStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export type RunYear = { id: string; year: number; source: string; provider: string | null };

export type Run = {
  id: string;
  status: RunStatus;
  base_year: RunYear;
  target_year: RunYear;
  threshold: number;
  min_new_area_m2: number;
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

export function listRuns(): Promise<Run[]> {
  return apiFetch<Run[]>("/api/runs");
}

export function createRun(input: {
  base_year_id: string;
  target_year_id: string;
  threshold?: number;
  min_new_area_m2?: number;
}): Promise<Run> {
  return apiFetch<Run>("/api/runs", { method: "POST", body: JSON.stringify(input) });
}

export function cancelRun(id: string): Promise<Run> {
  return apiFetch<Run>(`/api/runs/${id}/cancel`, { method: "POST" });
}
