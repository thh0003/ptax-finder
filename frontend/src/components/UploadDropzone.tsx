import { useRef, useState } from "react";
import type { ChangeEvent, DragEvent } from "react";

type Props = {
  onFiles: (files: File[]) => void;
  /** Accepted extensions, e.g. [".zip", ".geojson"]. */
  accept: string[];
  /** What the dropzone is for, e.g. "a zipped shapefile or GeoJSON". */
  label: string;
  /** Screen-reader label for the hidden file input. */
  inputLabel: string;
  /** Message when a file is rejected by extension. */
  rejectMessage: (name: string) => string;
  multiple?: boolean;
  /** null when idle, 0..1 while uploading. */
  progress: number | null;
  /** Shown under the progress bar while uploading (e.g. the current file name). */
  progressLabel?: string;
  disabled?: boolean;
};

export default function UploadDropzone({
  onFiles,
  accept,
  label,
  inputLabel,
  rejectMessage,
  multiple = false,
  progress,
  progressLabel,
  disabled,
}: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [rejected, setRejected] = useState<string | null>(null);

  function acceptFiles(list: FileList | null | undefined) {
    const files = Array.from(list ?? []);
    if (files.length === 0) return;
    const chosen = multiple ? files : files.slice(0, 1);
    const bad = chosen.find(
      (f) => !accept.some((ext) => f.name.toLowerCase().endsWith(ext.toLowerCase())),
    );
    if (bad) {
      setRejected(rejectMessage(bad.name));
      return;
    }
    setRejected(null);
    onFiles(chosen);
  }

  function onChange(event: ChangeEvent<HTMLInputElement>) {
    acceptFiles(event.target.files);
    event.target.value = "";
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    if (!disabled) acceptFiles(event.dataTransfer.files);
  }

  const uploading = progress !== null;

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
      className={`rounded-lg border-2 border-dashed p-6 text-center ${
        dragging ? "border-blue-500 bg-blue-50" : "border-slate-300 bg-white"
      }`}
      data-testid="upload-dropzone"
    >
      <input
        ref={inputRef}
        type="file"
        accept={accept.join(",")}
        multiple={multiple}
        onChange={onChange}
        disabled={disabled || uploading}
        className="sr-only"
        aria-label={inputLabel}
      />
      <p className="text-slate-700">
        Drop {label} here, or{" "}
        <button
          type="button"
          onClick={() => inputRef.current?.click()}
          disabled={disabled || uploading}
          className="text-blue-700 underline disabled:opacity-50"
        >
          {multiple ? "choose files" : "choose a file"}
        </button>
        .
      </p>
      {uploading && (
        <div className="mt-4" aria-label="Upload progress">
          <div className="h-2 w-full rounded bg-slate-200">
            <div
              className="h-2 rounded bg-blue-600 transition-all"
              style={{ width: `${Math.round(progress * 100)}%` }}
            />
          </div>
          <p className="mt-1 text-xs text-slate-600">
            {progressLabel ? `${progressLabel}: ` : ""}
            {Math.round(progress * 100)}% uploaded
          </p>
        </div>
      )}
      {rejected && (
        <p role="alert" className="mt-3 text-sm text-red-700">
          {rejected}
        </p>
      )}
    </div>
  );
}
