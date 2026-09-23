import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { listYears, yearLabel, type ImageryYear } from "../api/imagery";
import {
  getRunParcel,
  parcelImageUrls,
  type Run,
  type RunParcelDetail,
} from "../api/runs";
import { apiFetch } from "../api/client";
import { apiFetchBlob } from "../api/uploads";
import StatusChip from "../components/StatusChip";

/** Why a parcel could not be scored, in words a reviewer can act on. */
const SKIP_REASONS: Record<string, string> = {
  no_coverage_base: "No imagery covers this parcel in the base year.",
  no_coverage_target: "No imagery covers this parcel in the target year.",
  partial_coverage: "Imagery covers too little of this parcel to compare the two years.",
};

/** The measurements behind the score, in the order a reviewer reads them. */
const INDICATORS: { key: string; label: string; unit: string }[] = [
  { key: "structure_m2", label: "Largest new structure", unit: "m²" },
  { key: "new_builtup_m2", label: "New built-up area", unit: "m²" },
  { key: "veg_loss_m2", label: "Vegetation loss", unit: "m²" },
  { key: "resolution_m", label: "Compared at", unit: "m/px" },
];

type Loaded = { url: string | null; error: string | null };

/** Fetch an authenticated image once, revoking its object URL on unmount. */
function useImage(path: string | null): Loaded {
  const [state, setState] = useState<Loaded>({ url: null, error: null });

  useEffect(() => {
    if (!path) return;
    let revoked = false;
    let created: string | null = null;
    apiFetchBlob(path)
      .then((url) => {
        if (revoked) {
          if (url) URL.revokeObjectURL(url);
          return;
        }
        created = url;
        setState({ url, error: null });
      })
      .catch((err: unknown) =>
        setState({ url: null, error: err instanceof Error ? err.message : "Could not load" }),
      );
    return () => {
      revoked = true;
      if (created) URL.revokeObjectURL(created);
    };
  }, [path]);

  return state;
}

function Pane({ title, image, empty }: { title: string; image: Loaded; empty: string }) {
  return (
    <figure className="flex min-w-0 flex-1 flex-col gap-2">
      <figcaption className="text-sm font-medium text-slate-700">{title}</figcaption>
      <div className="flex aspect-square items-center justify-center rounded border border-slate-200 bg-slate-50">
        {image.error ? (
          <p className="p-4 text-center text-sm text-red-700">{image.error}</p>
        ) : image.url ? (
          <img src={image.url} alt={title} className="h-full w-full rounded object-contain" />
        ) : (
          <p className="p-4 text-center text-sm text-slate-500">{empty}</p>
        )}
      </div>
    </figure>
  );
}

export default function ParcelViewerPage() {
  const { runId = "", parcelId = "" } = useParams();
  const [run, setRun] = useState<Run | null>(null);
  const [parcel, setParcel] = useState<RunParcelDetail | null>(null);
  const [years, setYears] = useState<ImageryYear[]>([]);
  const [showMarkup, setShowMarkup] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([
      apiFetch<Run>(`/api/runs/${runId}`),
      getRunParcel(runId, parcelId),
      listYears(),
    ])
      .then(([loadedRun, loadedParcel, loadedYears]) => {
        setRun(loadedRun);
        setParcel(loadedParcel);
        setYears(loadedYears);
      })
      .catch((err: unknown) =>
        setError(err instanceof Error ? err.message : "Could not load this parcel"),
      );
  }, [runId, parcelId]);

  const urls =
    run && parcel
      ? parcelImageUrls({
          runId,
          parcelId,
          baseYearId: run.base_year.id,
          targetYearId: run.target_year.id,
        })
      : null;

  // Both target images load up front, so toggling the markup issues no new request.
  const base = useImage(urls?.base ?? null);
  const target = useImage(urls?.target ?? null);
  const overlay = useImage(parcel?.has_markup ? (urls?.overlay ?? null) : null);

  if (error) return <p className="text-sm text-red-700">{error}</p>;
  if (!run || !parcel) return <p className="text-sm text-slate-500">Loading…</p>;

  const yearOf = (id: string) => years.find((y) => y.id === id);
  const baseLabel = yearOf(run.base_year.id);
  const targetLabel = yearOf(run.target_year.id);
  const skipped = parcel.skipped_reason;

  return (
    <section className="flex flex-col gap-6">
      <header className="flex flex-wrap items-baseline justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Parcel {parcel.parcel_ref}</h1>
          <p className="text-sm text-slate-600">
            {run.base_year.year} → {run.target_year.year}{" "}
            <Link to="/runs" className="underline">
              back to runs
            </Link>
          </p>
        </div>
        <div className="flex items-center gap-3">
          {parcel.candidate ? <StatusChip status="flagged" /> : null}
          <span className="text-2xl font-semibold tabular-nums">
            {parcel.score === null ? "—" : parcel.score.toFixed(3)}
          </span>
        </div>
      </header>

      {skipped ? (
        <p
          className="rounded border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900"
          data-testid="skip-reason"
        >
          {SKIP_REASONS[skipped] ?? `This parcel was not scored (${skipped}).`} The years that
          do have imagery are shown below.
        </p>
      ) : null}

      <div className="flex flex-col gap-4 lg:flex-row" data-testid="panes">
        <Pane
          title={baseLabel ? yearLabel(baseLabel) : `${run.base_year.year}`}
          image={base}
          empty="No imagery for this year"
        />
        <Pane
          title={targetLabel ? yearLabel(targetLabel) : `${run.target_year.year}`}
          image={target}
          empty="No imagery for this year"
        />
        {parcel.has_markup ? (
          <Pane
            title={showMarkup ? "Detected change" : "Target year, no markup"}
            image={showMarkup ? overlay : target}
            empty="No imagery for this year"
          />
        ) : null}
      </div>

      {parcel.has_markup ? (
        <label className="flex w-fit items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={showMarkup}
            onChange={(e) => setShowMarkup(e.target.checked)}
            data-testid="markup-toggle"
          />
          Show detected change
        </label>
      ) : (
        <p className="text-sm text-slate-600" data-testid="no-markup">
          This run recorded no markup for this parcel
          {skipped ? "." : " — it detected no new structure, or it ran before markup was recorded."}
        </p>
      )}

      {parcel.indicators ? (
        <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-4">
          {INDICATORS.map(({ key, label, unit }) => {
            const value = parcel.indicators?.[key];
            return (
              <div key={key}>
                <dt className="text-slate-600">{label}</dt>
                <dd className="tabular-nums" data-testid={`indicator-${key}`}>
                  {value === null || value === undefined ? "—" : `${value} ${unit}`}
                </dd>
              </div>
            );
          })}
        </dl>
      ) : null}
    </section>
  );
}
