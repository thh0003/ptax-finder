import { useCallback, useEffect, useState } from "react";
import type { SyntheticEvent } from "react";
import { ApiError } from "../api/client";
import { listYears, yearLabel, type ImageryYear } from "../api/imagery";
import {
  DEFAULT_MIN_NEW_AREA_M2,
  DEFAULT_THRESHOLD,
  RUN_ACTIVE,
  cancelRun,
  createRun,
  listRuns,
  type Run,
} from "../api/runs";
import { useAuth } from "../auth/AuthContext";
import StatusChip from "../components/StatusChip";

const POLL_MS = 2000;

export default function RunsPage() {
  const { state } = useAuth();
  const isAdmin = state.status === "authenticated" && state.me.role === "admin";

  const [runs, setRuns] = useState<Run[] | null>(null);
  const [years, setYears] = useState<ImageryYear[]>([]);
  const [baseId, setBaseId] = useState("");
  const [targetId, setTargetId] = useState("");
  const [threshold, setThreshold] = useState(String(DEFAULT_THRESHOLD));
  const [minArea, setMinArea] = useState(String(DEFAULT_MIN_NEW_AREA_M2));
  const [advanced, setAdvanced] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    try {
      setRuns(await listRuns());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load runs");
    }
  }, []);

  useEffect(() => {
    void reload();
    listYears()
      .then((all) => setYears(all.filter((y) => y.status === "ready")))
      .catch(() => setYears([]));
  }, [reload]);

  const active = runs?.some((r) => RUN_ACTIVE.includes(r.status)) ?? false;
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => void reload(), POLL_MS);
    return () => clearInterval(id);
  }, [active, reload]);

  async function onStart(event: SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    setBusy(true);
    try {
      await createRun({
        base_year_id: baseId,
        target_year_id: targetId,
        threshold: Number(threshold),
        min_new_area_m2: Number(minArea),
      });
      await reload();
    } catch (err) {
      // 422s carry the exact reason (e.g. "Target year must be later than base year").
      setFormError(
        err instanceof ApiError ? err.detail : err instanceof Error ? err.message : "Could not start run",
      );
    } finally {
      setBusy(false);
    }
  }

  async function onCancel(run: Run) {
    setError(null);
    try {
      await cancelRun(run.id);
    } catch (err) {
      if (!(err instanceof ApiError && err.status === 409)) {
        setError(err instanceof Error ? err.message : "Could not cancel run");
      }
    } finally {
      await reload();
    }
  }

  if (runs === null) {
    return error ? (
      <p role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">
        {error}
      </p>
    ) : (
      <p className="text-slate-600">Loading...</p>
    );
  }

  const canStart = years.length >= 2;
  const sorted = [...years].sort((a, b) => a.year - b.year);

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold">Runs</h1>
      {error && (
        <p role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">
          {error}
        </p>
      )}

      {isAdmin && (
        <section aria-labelledby="start-heading" className="rounded-lg bg-white p-5 shadow">
          <h2 id="start-heading" className="mb-3 text-sm font-medium text-slate-600">
            Start a run
          </h2>
          {canStart ? (
            <form onSubmit={onStart} className="space-y-3">
              <div className="flex flex-wrap items-end gap-3">
                <label className="text-sm">
                  <span className="block text-slate-600">Base year</span>
                  <select
                    aria-label="Base year"
                    value={baseId}
                    onChange={(e) => setBaseId(e.target.value)}
                    className="rounded border border-slate-300 px-3 py-2"
                  >
                    <option value="">Select a year...</option>
                    {sorted.map((y) => (
                      <option key={y.id} value={y.id}>
                        {yearLabel(y)}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="text-sm">
                  <span className="block text-slate-600">Target year</span>
                  <select
                    aria-label="Target year"
                    value={targetId}
                    onChange={(e) => setTargetId(e.target.value)}
                    className="rounded border border-slate-300 px-3 py-2"
                  >
                    <option value="">Select a year...</option>
                    {sorted.map((y) => (
                      <option key={y.id} value={y.id}>
                        {yearLabel(y)}
                      </option>
                    ))}
                  </select>
                </label>
                <button
                  type="submit"
                  disabled={busy || !baseId || !targetId}
                  className="rounded bg-blue-700 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
                >
                  Start run
                </button>
              </div>
              <button
                type="button"
                onClick={() => setAdvanced((v) => !v)}
                className="text-sm text-blue-700 underline"
                aria-expanded={advanced}
              >
                Advanced
              </button>
              {advanced && (
                <div className="flex flex-wrap gap-3">
                  <label className="text-sm">
                    <span className="block text-slate-600">Candidate threshold (0-1)</span>
                    <input
                      type="number"
                      min={0}
                      max={1}
                      step={0.05}
                      value={threshold}
                      onChange={(e) => setThreshold(e.target.value)}
                      className="rounded border border-slate-300 px-3 py-2"
                    />
                  </label>
                  <label className="text-sm">
                    <span className="block text-slate-600">Minimum new built-up area (m²)</span>
                    <input
                      type="number"
                      min={0}
                      step={10}
                      value={minArea}
                      onChange={(e) => setMinArea(e.target.value)}
                      className="rounded border border-slate-300 px-3 py-2"
                    />
                  </label>
                </div>
              )}
              {formError && (
                <p role="alert" className="text-sm text-red-800" data-testid="run-form-error">
                  {formError}
                </p>
              )}
            </form>
          ) : (
            <p className="text-sm text-slate-700">
              A parcel layer and two ready imagery years are needed to start a run.
            </p>
          )}
        </section>
      )}

      <section aria-labelledby="runs-heading">
        <h2 id="runs-heading" className="mb-2 text-sm font-medium text-slate-600">
          Runs
        </h2>
        {runs.length === 0 ? (
          <p className="rounded-lg border border-slate-200 bg-white p-5 text-slate-700">
            No runs yet.
          </p>
        ) : (
          <ul className="divide-y divide-slate-200 rounded-lg bg-white shadow">
            {runs.map((run) => (
              <li key={run.id} className="space-y-1 px-4 py-3 text-sm" data-testid="run-row">
                <div className="flex flex-wrap items-center gap-3">
                  <span className="font-medium" data-testid="run-label">
                    {run.base_year.year} → {run.target_year.year}
                  </span>
                  <StatusChip status={run.status} testId="run-status" />
                  {RUN_ACTIVE.includes(run.status) && (
                    <span className="flex items-center gap-2" data-testid="run-progress">
                      <progress value={run.parcels_processed} max={run.parcels_total} />
                      {run.parcels_processed} / {run.parcels_total}
                    </span>
                  )}
                  {(run.status === "succeeded" || run.status === "cancelled") && (
                    <span className="text-slate-700" data-testid="run-summary">
                      {run.parcels_processed} processed · {run.candidates}{" "}
                      {run.candidates === 1 ? "candidate" : "candidates"} · {run.parcels_skipped}{" "}
                      skipped
                    </span>
                  )}
                  {isAdmin && RUN_ACTIVE.includes(run.status) && (
                    <button
                      type="button"
                      onClick={() => void onCancel(run)}
                      className="ml-auto rounded border border-slate-300 px-3 py-1 text-sm hover:bg-slate-100"
                    >
                      Cancel
                    </button>
                  )}
                </div>
                <p className="text-xs text-slate-500">
                  started {run.started_at ? new Date(run.started_at).toLocaleString() : "-"}
                  {run.finished_at && ` · finished ${new Date(run.finished_at).toLocaleString()}`}
                  {` · threshold ${run.threshold} · min ${run.min_new_area_m2} m²`}
                </p>
                {run.error && (
                  <p className="text-sm text-red-800" data-testid="run-error">
                    {run.error}
                  </p>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
