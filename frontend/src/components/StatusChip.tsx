const STYLES: Record<string, string> = {
  // waiting
  queued: "bg-slate-200 text-slate-800",
  pending: "bg-slate-200 text-slate-800",
  uploaded: "bg-slate-200 text-slate-800",
  // working
  processing: "bg-blue-100 text-blue-900",
  running: "bg-blue-100 text-blue-900",
  inspecting: "bg-blue-100 text-blue-900",
  ingesting: "bg-blue-100 text-blue-900",
  awaiting_field: "bg-blue-100 text-blue-900",
  // done
  ready: "bg-green-100 text-green-900",
  succeeded: "bg-green-100 text-green-900",
  failed: "bg-red-100 text-red-900",
  cancelled: "bg-amber-100 text-amber-900",
};

type Props = { status: string; testId?: string };

/** One chip for every status vocabulary in the app (layers, imagery years, assets, runs). */
export default function StatusChip({ status, testId = "status-chip" }: Props) {
  const style = STYLES[status] ?? "bg-slate-200 text-slate-800";
  return (
    <span
      className={`inline-block rounded px-2 py-0.5 text-xs font-medium ${style}`}
      data-testid={testId}
      data-status={status}
    >
      {status}
    </span>
  );
}
