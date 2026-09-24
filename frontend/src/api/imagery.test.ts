import { beforeEach, describe, expect, it, vi } from "vitest";

const calls: string[] = [];

vi.mock("./uploads", () => ({
  putPresigned: vi.fn(
    async (file: File, _p: string, _c: string, onProgress: (f: number) => void) => {
      calls.push(`put:${file.name}`);
      if (file.name === "bad.tif") throw new Error("Upload failed (500)");
      onProgress(1); // the real helper reports completion before resolving
      return { upload_id: "u", key: `tenants/t/imagery/u/${file.name}`, url: "x", expires_in: 1 };
    },
  ),
  apiFetchBlob: vi.fn(),
}));

vi.mock("./client", () => ({
  apiFetch: vi.fn(async (path: string, init?: RequestInit) => {
    const body = init?.body ? (JSON.parse(String(init.body)) as { original_filename: string }) : null;
    calls.push(`register:${body?.original_filename ?? path}`);
    return { id: "a", status: "pending", original_filename: body?.original_filename };
  }),
}));

import { contentTypeForImagery, uploadImageryFiles, yearLabel } from "./imagery";

beforeEach(() => calls.splice(0));

describe("uploadImageryFiles", () => {
  it("uploads and registers files one at a time, in order", async () => {
    const files = [new File(["a"], "a.tif"), new File(["b"], "b.tiff")];
    const progress: Array<[string, number]> = [];

    const assets = await uploadImageryFiles("year-1", files, (name, f) => progress.push([name, f]));

    expect(calls).toEqual(["put:a.tif", "register:a.tif", "put:b.tiff", "register:b.tiff"]);
    expect(assets.map((a) => a.original_filename)).toEqual(["a.tif", "b.tiff"]);
    expect(progress.at(-1)).toEqual(["b.tiff", 1]);
  });

  it("stops at the first failed upload without registering later files", async () => {
    const files = [new File(["a"], "a.tif"), new File(["x"], "bad.tif"), new File(["c"], "c.tif")];

    await expect(uploadImageryFiles("year-1", files, () => {})).rejects.toThrow("Upload failed");

    expect(calls).toEqual(["put:a.tif", "register:a.tif", "put:bad.tif"]);
  });
});

describe("contentTypeForImagery", () => {
  it("accepts .tif and .tiff only", () => {
    expect(contentTypeForImagery("ortho.TIF")).toBe("image/tiff");
    expect(contentTypeForImagery("ortho.tiff")).toBe("image/tiff");
    expect(contentTypeForImagery("parcels.geojson")).toBeNull();
  });
});

describe("yearLabel", () => {
  it("names a county ArcGIS year by its service, not its whole URL", () => {
    const provider = "https://gis.peoriacounty.gov/arcgis/rest/services/RL/Orthos2015/MapServer";
    expect(yearLabel({ year: 2015, source: "arcgis", provider })).toBe("2015 · arcgis (Orthos2015)");
  });

  it("keeps other providers as given", () => {
    expect(yearLabel({ year: 2021, source: "upload", provider: "Nearmap" })).toBe(
      "2021 · upload (Nearmap)",
    );
    expect(yearLabel({ year: 2021, source: "naip", provider: null })).toBe("2021 · naip");
  });
});
