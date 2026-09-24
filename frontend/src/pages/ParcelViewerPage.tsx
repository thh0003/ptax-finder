import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { listYears, yearLabel, type ImageryYear } from "../api/imagery";
import {
  detectorLabel,
  getRunParcel,
  indicatorsFor,
  inventoryOf,
  parcelImageUrls,
  runLabel,
  type Indicator,
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
  no_coverage: "No imagery covers this parcel in this year.",
  model_error: "The vision model did not give a usable answer for this parcel, even when asked twice.",
};

/** The county record fields a reviewer compares the inventory against, in reading order. */
const COUNTY_FIELDS: { key: string; label: string; unit?: string }[] = [
  { key: "year_built", label: "Year built" },
  { key: "eff_year_built", label: "Effective year built" },
  { key: "total_living_area", label: "Living area", unit: "sq ft" },
  { key: "gar_area", label: "Garage area", unit: "sq ft" },
  { key: "det_gar_area", label: "Detached garage area", unit: "sq ft" },
  { key: "PropClass", label: "Property class" },
];

function formatIndicator(value: unknown, indicator: Indicator): string {
  if (value === null || value === undefined) return "—";
  if (indicator.percent && typeof value === "number") return `${(value * 100).toFixed(1)}%`;
  return `${String(value)} ${indicator.unit}`;
}

function formatCounty(value: string | number | boolean | null | undefined, unit?: string): string {
  if (value === null || value === undefined || value === "") return "—";
  return unit ? `${String(value)} ${unit}` : String(value);
}

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
          targetYearId: run.target_year?.id ?? null,
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
  const skipped = parcel.skipped_reason;

  if (run.kind === "inventory" || run.target_year === null) {
    return (
      <InventoryParcel
        run={run}
        parcel={parcel}
        yearTitle={baseLabel ? yearLabel(baseLabel) : `${run.base_year.year}`}
        plain={base}
        overlay={overlay}
      />
    );
  }
  const targetYear = run.target_year;
  const targetLabel = yearOf(targetYear.id);

  return (
    <section className="flex flex-col gap-6">
      <header className="flex flex-wrap items-baseline justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Parcel {parcel.parcel_ref}</h1>
          <p className="text-sm text-slate-600">
            {runLabel(run)} · Detected by{" "}
            <span data-testid="detected-by">{detectorLabel(run)}</span>{" "}
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
          title={targetLabel ? yearLabel(targetLabel) : `${targetYear.year}`}
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
          {indicatorsFor(run.detector).map((indicator) => (
            <div key={indicator.key}>
              <dt className="text-slate-600">{indicator.label}</dt>
              <dd className="tabular-nums" data-testid={`indicator-${indicator.key}`}>
                {formatIndicator(parcel.indicators?.[indicator.key], indicator)}
              </dd>
            </div>
          ))}
        </dl>
      ) : null}
    </section>
  );
}

/**
 * An inventory parcel: its one year, with the model's structures outlined (toggleable),
 * what the model said, and the county's record to check it against.
 */
function InventoryParcel({
  run,
  parcel,
  yearTitle,
  plain,
  overlay,
}: {
  run: Run;
  parcel: RunParcelDetail;
  yearTitle: string;
  plain: Loaded;
  overlay: Loaded;
}) {
  const [showMarkup, setShowMarkup] = useState(true);
  const { structures, summary } = inventoryOf(parcel.indicators);
  const skipped = parcel.skipped_reason;
  const marked = parcel.has_markup && showMarkup;

  return (
    <section className="flex flex-col gap-6">
      <header>
        <h1 className="text-xl font-semibold">Parcel {parcel.parcel_ref}</h1>
        <p className="text-sm text-slate-600">
          <span data-testid="run-label">{runLabel(run)}</span>{" "}
          <Link to="/runs" className="underline">
            back to runs
          </Link>
        </p>
      </header>

      {skipped ? (
        <p
          className="rounded border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900"
          data-testid="skip-reason"
        >
          {SKIP_REASONS[skipped] ?? `This parcel was not read (${skipped}).`}
        </p>
      ) : null}

      <div className="flex flex-col gap-6 lg:flex-row">
        <div className="flex max-w-xl flex-1 flex-col gap-2" data-testid="panes">
          <Pane
            title={marked ? `${yearTitle}, structures marked` : yearTitle}
            image={marked ? overlay : plain}
            empty="No imagery for this year"
          />
          {parcel.has_markup ? (
            <label className="flex w-fit items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={showMarkup}
                onChange={(e) => setShowMarkup(e.target.checked)}
                data-testid="markup-toggle"
              />
              Show structures
            </label>
          ) : null}
        </div>

        <div className="flex flex-1 flex-col gap-5 text-sm">
          {summary ? (
            <p className="text-slate-800" data-testid="inventory-summary">
              {summary}
            </p>
          ) : null}
          {!skipped ? (
            structures.length > 0 ? (
              <table className="w-full max-w-sm text-left" data-testid="structures">
                <thead className="text-xs text-slate-500">
                  <tr>
                    <th className="py-1 font-medium">Structure</th>
                    <th className="py-1 font-medium">Confidence</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {structures.map((structure, index) => (
                    <tr key={index} data-testid="structure-row">
                      <td className="py-1">{structure.kind}</td>
                      <td className="py-1 tabular-nums">{structure.confidence.toFixed(2)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="text-slate-600" data-testid="no-structures">
                The model found no structures on this parcel.
              </p>
            )
          ) : null}

          <div>
            <h2 className="mb-1 text-sm font-medium text-slate-600">County record</h2>
            {parcel.parcel_attributes ? (
              <dl className="grid max-w-sm grid-cols-2 gap-x-6 gap-y-1" data-testid="county-record">
                {COUNTY_FIELDS.map((field) => (
                  <div key={field.key} className="contents">
                    <dt className="text-slate-600">{field.label}</dt>
                    <dd className="tabular-nums">
                      {formatCounty(parcel.parcel_attributes?.[field.key], field.unit)}
                    </dd>
                  </div>
                ))}
              </dl>
            ) : (
              <p className="text-slate-600">No county record for this parcel.</p>
            )}
          </div>

          <p className="text-xs text-slate-500">
            Read by <span data-testid="detected-by">{detectorLabel(run)}</span>
            {typeof parcel.indicators?.resolution_m === "number"
              ? ` at ${parcel.indicators.resolution_m} m/px`
              : ""}
          </p>
        </div>
      </div>
    </section>
  );
}
