import { apiFetch } from "./client";
import { putPresigned } from "./uploads";

export type LayerStatusValue =
  | "uploaded"
  | "inspecting"
  | "awaiting_field"
  | "ingesting"
  | "ready"
  | "failed";

export type LayerField = { name: string; dtype: string; samples: string[] };

export type ParcelLayer = {
  id: string;
  status: LayerStatusValue;
  original_filename: string;
  s3_key: string;
  parcel_id_field: string | null;
  fields: LayerField[] | null;
  source_crs: string | null;
  feature_count: number | null;
  skipped_count: number | null;
  error: string | null;
  is_current: boolean;
  created_at: string;
};

export const IN_PROGRESS: LayerStatusValue[] = ["uploaded", "inspecting", "ingesting"];

export const ACCEPTED_EXTENSIONS = [".zip", ".geojson", ".json"];

export function contentTypeFor(filename: string): string | null {
  const lower = filename.toLowerCase();
  if (lower.endsWith(".zip")) return "application/zip";
  if (lower.endsWith(".geojson") || lower.endsWith(".json")) return "application/geo+json";
  return null;
}

export function listLayers(): Promise<ParcelLayer[]> {
  return apiFetch<ParcelLayer[]>("/api/parcel-layers");
}

export function getLayer(id: string): Promise<ParcelLayer> {
  return apiFetch<ParcelLayer>(`/api/parcel-layers/${id}`);
}

export function ingestLayer(id: string, parcelIdField: string): Promise<ParcelLayer> {
  return apiFetch<ParcelLayer>(`/api/parcel-layers/${id}/ingest`, {
    method: "POST",
    body: JSON.stringify({ parcel_id_field: parcelIdField }),
  });
}

/** Presigned PUT straight to S3/MinIO with progress, then register the layer. */
export async function uploadParcelLayer(
  file: File,
  onProgress: (fraction: number) => void,
): Promise<ParcelLayer> {
  const contentType = contentTypeFor(file.name);
  if (!contentType) throw new Error(`Unsupported file type; use ${ACCEPTED_EXTENSIONS.join(", ")}`);
  const ticket = await putPresigned(file, "parcel_layer", contentType, onProgress);

  return apiFetch<ParcelLayer>("/api/parcel-layers", {
    method: "POST",
    body: JSON.stringify({ upload_key: ticket.key, original_filename: file.name }),
  });
}
