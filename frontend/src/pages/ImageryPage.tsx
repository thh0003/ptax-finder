import { useCallback, useEffect, useState } from "react";
import type { SyntheticEvent } from "react";
import { ApiError } from "../api/client";
import {
  IMAGERY_EXTENSIONS,
  YEAR_IN_PROGRESS,
  createUploadYear,
  finalizeYear,
  ingestNaip,
  listYears,
  naipAvailable,
  thumbnailPath,
  uploadImageryFiles,
  yearLabel,
  type ImageryYear,
  type NaipAvailability,
} from "../api/imagery";
import { apiFetchBlob } from "../api/uploads";
import { useAuth } from "../auth/AuthContext";
import StatusChip from "../components/StatusChip";
import UploadDropzone from "../components/UploadDropzone";

const POLL_MS = 2000;

function ErrorBanner({ message }: { message: string }) {
  return (
    <p role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">
      {message}
    </p>
  );
}

function Thumbnail({ yearId }: { yearId: string }) {
  const [src, setSrc] = useState<string | null>(null);
  useEffect(() => {
    let url: string | null = null;
    let cancelled = false;
    apiFetchBlob(thumbnailPath(yearId))
      .then((u) => {
        url = u;
        if (!cancelled) setSrc(u);
      })
      .catch(() => setSrc(null));
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [yearId]);
  if (!src) return <div className="h-16 w-16 rounded bg-slate-100" aria-hidden="true" />;
  return (
    <img
      src={src}
      alt="Imagery thumbnail"
      className="h-16 w-16 rounded object-cover"
      data-testid="year-thumbnail"
    />
  );
}

function YearRow({ year }: { year: ImageryYear }) {
  return (
    <>
      <div className="flex flex-wrap items-center gap-3">
        <span className="font-medium" data-testid="year-label">
          {yearLabel(year)}
        </span>
        <StatusChip status={year.status} testId="year-status" />
        {year.status === "ready" && (
          <span className="text-sm text-slate-700" data-testid="year-summary">
            {year.band_count} bands · {year.resolution_m?.toFixed(1)} m · coverage{" "}
            {Math.round(year.coverage_pct ?? 0)}% · {year.parcels_uncovered ?? 0} parcels without
            coverage
          </span>
        )}
      </div>
      {year.error && (
        <p className="mt-1 text-sm text-red-800" data-testid="year-error">
          {year.error}
        </p>
      )}
    </>
  );
}

export default function ImageryPage() {
  const { state } = useAuth();
  const isAdmin = state.status === "authenticated" && state.me.role === "admin";

  const [years, setYears] = useState<ImageryYear[] | null>(null);
  const [naip, setNaip] = useState<NaipAvailability[] | null>(null);
  const [noLayer, setNoLayer] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmYear, setConfirmYear] = useState<NaipAvailability | null>(null);
  const [newYear, setNewYear] = useState("");
  const [provider, setProvider] = useState("");
  const [busy, setBusy] = useState(false);
  const [upload, setUpload] = useState<{ yearId: string; file: string; fraction: number } | null>(
    null,
  );

  const reload = useCallback(async () => {
    try {
      setYears(await listYears());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load imagery");
    }
  }, []);

  const loadNaip = useCallback(async () => {
    try {
      setNaip(await naipAvailable());
      setNoLayer(false);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) setNoLayer(true);
      else setError(err instanceof Error ? err.message : "Could not load NAIP availability");
    }
  }, []);

  useEffect(() => {
    void reload();
    void loadNaip();
  }, [reload, loadNaip]);

  const inProgress = years?.some((y) => YEAR_IN_PROGRESS.includes(y.status)) ?? false;
  useEffect(() => {
    if (!inProgress) return;
    const id = setInterval(() => void reload(), POLL_MS);
    return () => clearInterval(id);
  }, [inProgress, reload]);

  // Once a NAIP year finishes, the availability list needs its `existing` status refreshed.
  useEffect(() => {
    if (!inProgress && years) void loadNaip();
  }, [inProgress, years, loadNaip]);

  async function run(action: () => Promise<unknown>, fallback: string) {
    setError(null);
    setBusy(true);
    try {
      await action();
    } catch (err) {
      setError(err instanceof Error ? err.message : fallback);
    } finally {
      setBusy(false);
      await reload();
    }
  }

  function onIngest(item: NaipAvailability) {
    setConfirmYear(null);
    void run(() => ingestNaip(item.year), "Could not start NAIP ingest");
  }

  function onCreateYear(event: SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();
    const year = Number(newYear);
    if (!Number.isInteger(year)) return;
    void run(async () => {
      await createUploadYear(year, provider.trim() || null);
      setNewYear("");
      setProvider("");
    }, "Could not create year");
  }

  function onFiles(year: ImageryYear, files: File[]) {
    void run(async () => {
      await uploadImageryFiles(year.id, files, (file, fraction) =>
        setUpload({ yearId: year.id, file, fraction }),
      );
    }, "Upload failed").finally(() => setUpload(null));
  }

  function onFinalize(year: ImageryYear) {
    void run(() => finalizeYear(year.id), "Could not finish upload");
  }

  if (years === null) {
    return error ? <ErrorBanner message={error} /> : <p className="text-slate-600">Loading...</p>;
  }

  if (noLayer && years.length === 0) {
    return (
      <div className="space-y-6">
        <h1 className="text-xl font-semibold">Imagery</h1>
        <p className="rounded-lg border border-slate-200 bg-white p-5 text-slate-700">
          No parcel layer yet — upload parcels before imagery.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold">Imagery</h1>
      {error && <ErrorBanner message={error} />}

      <section aria-labelledby="years-heading">
        <h2 id="years-heading" className="mb-2 text-sm font-medium text-slate-600">
          Imagery years
        </h2>
        {years.length === 0 ? (
          <p className="rounded-lg border border-slate-200 bg-white p-5 text-slate-700">
            No imagery years yet.
          </p>
        ) : (
          <ul className="divide-y divide-slate-200 rounded-lg bg-white shadow">
            {years.map((year) => (
              <li key={year.id} className="flex gap-4 px-4 py-3" data-testid="year-row">
                {year.status === "ready" ? (
                  <Thumbnail yearId={year.id} />
                ) : (
                  <div className="h-16 w-16 rounded bg-slate-100" aria-hidden="true" />
                )}
                <div className="min-w-0 flex-1 space-y-2">
                  <YearRow year={year} />
                  {year.source === "upload" && (
                    <UploadYearFiles
                      year={year}
                      isAdmin={isAdmin}
                      busy={busy}
                      upload={upload?.yearId === year.id ? upload : null}
                      onFiles={(files) => onFiles(year, files)}
                      onFinalize={() => onFinalize(year)}
                    />
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="naip-heading">
        <h2 id="naip-heading" className="mb-2 text-sm font-medium text-slate-600">
          NAIP on AWS
        </h2>
        {naip === null ? (
          <p className="text-slate-600">Checking NAIP coverage for the county...</p>
        ) : naip.length === 0 ? (
          <p className="text-slate-700">No NAIP imagery covers this county.</p>
        ) : (
          <ul className="divide-y divide-slate-200 rounded-lg bg-white shadow">
            {naip.map((item) => (
              <li
                key={item.year}
                className="flex flex-wrap items-center gap-3 px-4 py-3 text-sm"
                data-testid="naip-row"
              >
                <span className="font-medium">{item.year}</span>
                <span className="text-slate-700">
                  coverage {item.coverage_pct}% · {item.item_count}{" "}
                  {item.item_count === 1 ? "tile" : "tiles"} · {item.gsd_m.toFixed(1)} m · ~
                  {formatGb(item.estimated_gb)} GB
                </span>
                {item.existing && (
                  <span className="text-slate-600" data-testid="naip-existing">
                    {item.existing.status === "ready" ? "ingested" : item.existing.status}
                  </span>
                )}
                {isAdmin && (
                  <button
                    type="button"
                    disabled={busy || (item.existing !== null && item.existing.status !== "failed")}
                    onClick={() => setConfirmYear(item)}
                    className="ml-auto rounded bg-blue-700 px-3 py-1 text-sm font-medium text-white disabled:opacity-50"
                  >
                    {item.existing?.status === "failed" ? "Retry" : "Ingest"}
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      {confirmYear && (
        <div
          role="dialog"
          aria-labelledby="confirm-heading"
          className="rounded-lg border border-blue-200 bg-blue-50 p-4"
        >
          <h3 id="confirm-heading" className="font-medium">
            Ingest NAIP {confirmYear.year}?
          </h3>
          <p className="mt-1 text-sm text-slate-700">
            Copies about {formatGb(confirmYear.estimated_gb)} GB ({confirmYear.item_count}{" "}
            {confirmYear.item_count === 1 ? "tile" : "tiles"}) from the requester-pays NAIP bucket
            into this county's imagery store.
          </p>
          <div className="mt-3 flex gap-2">
            <button
              type="button"
              onClick={() => onIngest(confirmYear)}
              className="rounded bg-blue-700 px-3 py-1 text-sm font-medium text-white"
            >
              Confirm ingest
            </button>
            <button
              type="button"
              onClick={() => setConfirmYear(null)}
              className="rounded border border-slate-300 px-3 py-1 text-sm"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {isAdmin && (
        <section aria-labelledby="upload-heading">
          <h2 id="upload-heading" className="mb-2 text-sm font-medium text-slate-600">
            Upload imagery
          </h2>
          <form onSubmit={onCreateYear} className="flex flex-wrap items-end gap-3">
            <label className="text-sm">
              <span className="block text-slate-600">Year</span>
              <input
                type="number"
                min={1990}
                max={2100}
                required
                value={newYear}
                onChange={(e) => setNewYear(e.target.value)}
                className="rounded border border-slate-300 px-3 py-2"
              />
            </label>
            <label className="text-sm">
              <span className="block text-slate-600">Provider (optional)</span>
              <input
                type="text"
                value={provider}
                onChange={(e) => setProvider(e.target.value)}
                placeholder="e.g. Nearmap"
                className="rounded border border-slate-300 px-3 py-2"
              />
            </label>
            <button
              type="submit"
              disabled={busy || !newYear}
              className="rounded bg-blue-700 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
            >
              Create year
            </button>
          </form>
          <p className="mt-2 text-xs text-slate-600">
            GeoTIFF or COG, 3 or 4 bands, 1 m/px or finer, up to 5 GB per file. Add files to
            the year above, then click Finish upload.
          </p>
        </section>
      )}
    </div>
  );
}

function UploadYearFiles({
  year,
  isAdmin,
  busy,
  upload,
  onFiles,
  onFinalize,
}: {
  year: ImageryYear;
  isAdmin: boolean;
  busy: boolean;
  upload: { file: string; fraction: number } | null;
  onFiles: (files: File[]) => void;
  onFinalize: () => void;
}) {
  const canAddFiles = isAdmin && year.status !== "processing";
  const hasPending = year.assets.some((a) => a.status === "pending");
  return (
    <div className="space-y-2">
      {year.assets.length > 0 && (
        <ul className="text-sm" data-testid="asset-list">
          {year.assets.map((asset) => (
            <li key={asset.id} className="flex flex-wrap items-center gap-2">
              <StatusChip status={asset.status} testId="asset-status" />
              <span>{asset.original_filename ?? asset.source_ref}</span>
              {asset.error && <span className="text-red-800">{asset.error}</span>}
            </li>
          ))}
        </ul>
      )}
      {canAddFiles && (
        <UploadDropzone
          onFiles={onFiles}
          accept={IMAGERY_EXTENSIONS}
          multiple
          label={`GeoTIFF/COG files for ${year.year}`}
          inputLabel={`GeoTIFF/COG files for ${year.year}`}
          rejectMessage={(name) => `${name} is not a GeoTIFF (.tif/.tiff)`}
          progress={upload ? upload.fraction : null}
          progressLabel={upload?.file}
          disabled={busy}
        />
      )}
      {isAdmin && (
        <button
          type="button"
          disabled={busy || !hasPending || year.status === "processing"}
          onClick={onFinalize}
          className="rounded bg-blue-700 px-3 py-1 text-sm font-medium text-white disabled:opacity-50"
        >
          Finish upload
        </button>
      )}
    </div>
  );
}

function formatGb(gb: number): string {
  return gb < 0.1 ? gb.toFixed(3) : gb.toFixed(1);
}
