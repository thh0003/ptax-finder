import { apiFetch, getTokens } from "./client";

export type UploadPurpose = "parcel_layer" | "imagery";

export type UploadTicket = { upload_id: string; key: string; url: string; expires_in: number };

/** Presigned PUT straight to S3/MinIO with upload progress; returns the storage ticket. */
export async function putPresigned(
  file: File,
  purpose: UploadPurpose,
  contentType: string,
  onProgress: (fraction: number) => void,
): Promise<UploadTicket> {
  const ticket = await apiFetch<UploadTicket>("/api/uploads", {
    method: "POST",
    body: JSON.stringify({ filename: file.name, content_type: contentType, purpose }),
  });

  await new Promise<void>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", ticket.url);
    xhr.setRequestHeader("Content-Type", contentType);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    };
    xhr.onload = () =>
      xhr.status >= 200 && xhr.status < 300
        ? resolve()
        : reject(new Error(`Upload failed (${xhr.status})`));
    xhr.onerror = () => reject(new Error("Upload failed (network error)"));
    xhr.send(file);
  });
  onProgress(1);
  return ticket;
}

/**
 * Fetch an authenticated image (tiles, thumbnails, previews) as an object URL.
 * `<img>` cannot send the bearer token, so the bytes come through fetch; callers revoke
 * the URL with `URL.revokeObjectURL` when done. Resolves to null on 204 (no imagery).
 */
export async function apiFetchBlob(path: string): Promise<string | null> {
  const token = getTokens()?.accessToken;
  const response = await fetch(path, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (response.status === 204) return null;
  if (!response.ok) throw new Error(`Could not load image (${response.status})`);
  return URL.createObjectURL(await response.blob());
}
