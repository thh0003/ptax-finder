import { apiFetch } from "./client";
import { putPresigned } from "./uploads";

export type ImageryYearStatus = "queued" | "processing" | "ready" | "failed";
export type ImageryAssetStatus = "pending" | "ready" | "failed";

export type ImageryAsset = {
  id: string;
  status: ImageryAssetStatus;
  original_filename: string | null;
  source_ref: string | null;
  error: string | null;
};

export type ImageryYear = {
  id: string;
  year: number;
  source: "naip" | "upload";
  provider: string | null;
  status: ImageryYearStatus;
  band_count: number | null;
  resolution_m: number | null;
  coverage_pct: number | null;
  parcels_uncovered: number | null;
  bounds: [number, number, number, number] | null;
  min_zoom: number;
  max_zoom: number;
  error: string | null;
  created_at: string;
  assets: ImageryAsset[];
};

export type NaipAvailability = {
  year: number;
  item_count: number;
  coverage_pct: number;
  estimated_gb: number;
  gsd_m: number;
  existing: { id: string; status: ImageryYearStatus } | null;
};

export const YEAR_IN_PROGRESS: ImageryYearStatus[] = ["queued", "processing"];
export const IMAGERY_EXTENSIONS = [".tif", ".tiff"];

export function contentTypeForImagery(filename: string): string | null {
  const lower = filename.toLowerCase();
  return lower.endsWith(".tif") || lower.endsWith(".tiff") ? "image/tiff" : null;
}

export function yearLabel(year: { year: number; source: string; provider?: string | null }): string {
  const provider = year.provider ? ` (${year.provider})` : "";
  return `${year.year} · ${year.source}${provider}`;
}

export function listYears(): Promise<ImageryYear[]> {
  return apiFetch<ImageryYear[]>("/api/imagery/years");
}

export function naipAvailable(): Promise<NaipAvailability[]> {
  return apiFetch<NaipAvailability[]>("/api/imagery/naip/available");
}

export function ingestNaip(year: number): Promise<ImageryYear> {
  return apiFetch<ImageryYear>("/api/imagery/naip/ingest", {
    method: "POST",
    body: JSON.stringify({ year }),
  });
}

export function createUploadYear(year: number, provider: string | null): Promise<ImageryYear> {
  return apiFetch<ImageryYear>("/api/imagery/years", {
    method: "POST",
    body: JSON.stringify({ year, provider: provider || null }),
  });
}

export function registerAsset(
  yearId: string,
  uploadKey: string,
  originalFilename: string,
): Promise<ImageryAsset> {
  return apiFetch<ImageryAsset>(`/api/imagery/years/${yearId}/assets`, {
    method: "POST",
    body: JSON.stringify({ upload_key: uploadKey, original_filename: originalFilename }),
  });
}

export function finalizeYear(yearId: string): Promise<ImageryYear> {
  return apiFetch<ImageryYear>(`/api/imagery/years/${yearId}/finalize`, { method: "POST" });
}

export function thumbnailPath(yearId: string): string {
  return `/api/imagery/years/${yearId}/thumbnail.png`;
}

/**
 * Upload GeoTIFFs for a year one at a time (a failed file stops the sequence so the
 * admin sees exactly which one failed), registering each before starting the next.
 */
export async function uploadImageryFiles(
  yearId: string,
  files: File[],
  onFileProgress: (filename: string, fraction: number) => void,
): Promise<ImageryAsset[]> {
  const assets: ImageryAsset[] = [];
  for (const file of files) {
    const contentType = contentTypeForImagery(file.name);
    if (!contentType) throw new Error(`${file.name} is not a GeoTIFF (.tif/.tiff)`);
    const ticket = await putPresigned(file, "imagery", contentType, (fraction) =>
      onFileProgress(file.name, fraction),
    );
    assets.push(await registerAsset(yearId, ticket.key, file.name));
  }
  return assets;
}
