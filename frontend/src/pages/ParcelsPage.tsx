import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../api/client";
import {
  ACCEPTED_EXTENSIONS,
  IN_PROGRESS,
  ingestLayer,
  listLayers,
  uploadParcelLayer,
  type ParcelLayer,
} from "../api/parcelLayers";
import { useAuth } from "../auth/AuthContext";
import LayerStatus from "../components/LayerStatus";
import UploadDropzone from "../components/UploadDropzone";

const POLL_MS = 2000;

export default function ParcelsPage() {
  const { state } = useAuth();
  const isAdmin = state.status === "authenticated" && state.me.role === "admin";

  const [layers, setLayers] = useState<ParcelLayer[] | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [field, setField] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    try {
      setLayers(await listLayers());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load parcel layers");
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  // Poll while any layer is being inspected or ingested.
  const inProgress = layers?.some((l) => IN_PROGRESS.includes(l.status)) ?? false;
  useEffect(() => {
    if (!inProgress) return;
    const id = setInterval(() => void reload(), POLL_MS);
    return () => clearInterval(id);
  }, [inProgress, reload]);

  async function onFile(file: File) {
    setError(null);
    setProgress(0);
    try {
      await uploadParcelLayer(file, setProgress);
      await reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setProgress(null);
    }
  }

  async function onIngest(layer: ParcelLayer) {
    setError(null);
    setBusy(true);
    try {
      await ingestLayer(layer.id, field);
    } catch (err) {
      // 409: the layer moved on (e.g. another admin already started it) — just refresh.
      if (!(err instanceof ApiError && err.status === 409)) {
        setError(err instanceof Error ? err.message : "Could not start ingest");
      }
    } finally {
      setBusy(false);
      await reload();
    }
  }

  if (layers === null) {
    return error ? (
      <p role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">
        {error}
      </p>
    ) : (
      <p className="text-slate-600">Loading...</p>
    );
  }

  const current = layers.find((l) => l.is_current) ?? null;
  const latest = layers[0] ?? null;
  // The layer the admin is working on: the newest one unless it is the ready current layer.
  const active = latest && latest.status !== "ready" ? latest : null;

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold">Parcels</h1>

      {error && (
        <p role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">
          {error}
        </p>
      )}

      {current ? (
        <section className="rounded-lg bg-white p-5 shadow" aria-labelledby="current-layer">
          <h2 id="current-layer" className="text-sm font-medium text-slate-600">
            Current parcel layer
          </h2>
          <p className="mt-1 text-lg font-semibold" data-testid="parcel-count">
            {current.feature_count !== null && current.skipped_count !== null
              ? `${current.feature_count - current.skipped_count} parcels`
              : "parcel count unavailable"}
          </p>
          <p className="text-sm text-slate-700">
            <span data-testid="skipped-count">{current.skipped_count ?? 0} skipped</span>
            {" · "}
            <span data-testid="crs">
              CRS {current.source_crs ?? "unknown"} {"→"} 4326
            </span>
            {" · "}
            ID field <code>{current.parcel_id_field}</code>
            {" · "}
            {current.original_filename}
          </p>
        </section>
      ) : (
        !active && (
          <p className="rounded-lg border border-slate-200 bg-white p-5 text-slate-700">
            No parcel layer yet.{" "}
            {isAdmin ? "Upload the county's parcel boundaries to get started." : ""}
          </p>
        )
      )}

      {active && (
        <section className="rounded-lg bg-white p-5 shadow space-y-3" aria-labelledby="active-layer">
          <div className="flex items-center gap-3">
            <h2 id="active-layer" className="text-sm font-medium text-slate-600">
              {active.original_filename}
            </h2>
            <LayerStatus status={active.status} />
          </div>
          {active.status === "failed" && (
            <p className="text-sm text-red-800" data-testid="layer-error">
              {active.error}
            </p>
          )}
          {active.status === "awaiting_field" && active.fields && (
            <div className="space-y-2">
              <p className="text-sm text-slate-700">
                {active.feature_count} features in {active.source_crs ?? "unknown CRS"}. Choose the
                attribute that holds the county's parcel identifier.
              </p>
              {isAdmin ? (
                <div className="flex items-center gap-3">
                  <label className="text-sm">
                    <span className="sr-only">Parcel ID field</span>
                    <select
                      value={field}
                      onChange={(e) => setField(e.target.value)}
                      className="rounded border border-slate-300 px-3 py-2"
                      aria-label="Parcel ID field"
                    >
                      <option value="">Select a field...</option>
                      {active.fields.map((f) => (
                        <option key={f.name} value={f.name}>
                          {f.name} ({f.dtype}) e.g. {f.samples.slice(0, 2).join(", ")}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    type="button"
                    disabled={!field || busy}
                    onClick={() => void onIngest(active)}
                    className="rounded bg-blue-700 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
                  >
                    Ingest
                  </button>
                </div>
              ) : (
                <p className="text-sm text-slate-600">An admin needs to choose the ID field.</p>
              )}
            </div>
          )}
          {IN_PROGRESS.includes(active.status) && (
            <p className="text-sm text-slate-600">Working... this page refreshes automatically.</p>
          )}
        </section>
      )}

      {isAdmin && (
        <section aria-labelledby="upload-heading">
          <h2 id="upload-heading" className="mb-2 text-sm font-medium text-slate-600">
            {current ? "Upload a newer parcel layer" : "Upload parcel layer"}
          </h2>
          <UploadDropzone
            onFiles={(files) => void onFile(files[0])}
            accept={ACCEPTED_EXTENSIONS}
            label="a zipped shapefile or GeoJSON"
            inputLabel="Parcel layer file"
            rejectMessage={(name) => `${name} is not a .zip shapefile bundle or GeoJSON file`}
            progress={progress}
            disabled={busy}
          />
        </section>
      )}

      {layers.length > 0 && (
        <section aria-labelledby="history-heading">
          <h2 id="history-heading" className="mb-2 text-sm font-medium text-slate-600">
            Layer history
          </h2>
          <ul className="divide-y divide-slate-200 rounded-lg bg-white shadow">
            {layers.map((layer) => (
              <li key={layer.id} className="flex items-center gap-3 px-4 py-3 text-sm">
                <LayerStatus status={layer.status} />
                <span className="font-medium">{layer.original_filename}</span>
                <span className="text-slate-500">
                  {new Date(layer.created_at).toLocaleString()}
                </span>
                {layer.feature_count !== null && (
                  <span className="text-slate-500">{layer.feature_count} features</span>
                )}
                {layer.is_current && (
                  <span className="ml-auto rounded bg-green-100 px-2 py-0.5 text-xs text-green-900">
                    current
                  </span>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
