# Structure Inventory from County ArcGIS Imagery with a Local Vision Model

> **Superseded 2026-09-24** by `docs/prd/2026-09-24-parcel-improvement-detection.md` (user decision). Kept as the record of what was built and measured.

Created: 2026-09-23
Author: tholmes4005@gmail.com
Agent: Claude Code
Category: Feature
Status: Draft
Research: Standard

## Problem Statement

The detector is live and it is still not good enough. `segmenter-v1` is the production default (`docs/plans/2026-09-23-segmenter-production-integration.md`): average precision 0.588, and 61% precision at a 14% flag rate on the 300 labelled Hennepin parcels. In use it fails in three concrete ways:

- **It misses small structures.** Sheds, garages, additions and pools fall below what 1.0 m NAIP can show.
- **It flags the wrong things.** Driveways, shadows, bare ground and houses it failed to see in the base year all appear in the queue.
- **Its markup is sloppy.** The outlines spill off roofs onto driveways.

All three trace back to what the detector is shown and how well it recognises a building.

The new direction is to use two things together:
- **County imagery.** Many counties publish their own orthophotos and parcels through Esri ArcGIS. Peoria County's are tiled to about 0.15 m, up to 7× finer than NAIP, and it publishes exact parcel shapes.
- **A vision-language model.** The user's own `qwen3-vl`, which reads a scene rather than classifying pixels.

Before that combination is trusted to find *new* structures, it has to prove the simpler thing: **given one image of a parcel, can it correctly identify the structures already there?** Change detection is only as good as that. A model that misses the base year's garage will call it new in the target year; that is exactly the false positive `segmenter-v1` makes today.

This PRD covers that first step only. Scan the **2015** Peoria orthophoto for every parcel in **Richwoods Township**, inventory the structures the model sees, and measure that inventory against the user's own judgement.

## Core User Flows

### Flow 1: Set up the county
1. The user adds Peoria County to the locally running ptax-finder app as a county-ArcGIS source:
   - its parcel service;
   - which parcel fields mean what (parcel id, year built, garage area, detached garage area, living area);
   - its 2015 orthophoto service.
2. The app loads the Richwoods Township parcels (3,242) from the county's service and registers the 2015 imagery year. It shows that year's resolution.

### Flow 2: Run a structure inventory
1. On the locally running app, the user starts a **structure inventory** over Richwoods on the 2015 imagery, using the `qwen3-vl` model. The local worker runs it: resumably, in batches, with progress, like today's runs.
2. For each parcel the model sees the 2015 image cut to the parcel, with its boundary drawn on. It reports the structures inside the parcel, each with:
   - its kind: house, garage, shed or outbuilding, pool, other;
   - where it is on the image;
   - how confident the model is.
   It also reports a short summary, for example "1 house, 1 detached garage", or "no structures".
3. When the run finishes the app reports throughput: parcels per minute, and a projection for the whole county.

### Flow 3: Review the inventory in the app
1. The user opens the inventory run's parcel list and filters it by structure kind, or by "no structures".
2. The parcel viewer shows:
   - the 2015 image with the parcel outline and the model's structures marked on it;
   - the model's summary;
   - the county's own record: year built, living area, garage and detached-garage area.

### Flow 4: Measure the inventory
1. The user labels a sample of Richwoods parcels by eye on contact sheets, as was done for Hennepin: which structures each parcel really has.
2. The evaluation harness compares the model's inventory with those labels, by structure kind:
   - per parcel, was each kind present or absent;
   - were the counts right;
   - where it can be judged, was each structure marked in the right place.
3. It also reports the inventory's agreement with the county's records, as a secondary check:
   - a parcel with a recorded house should show one;
   - a recorded detached garage should show a separate structure;
   - a vacant parcel should show none.

## Scope

### In Scope
- **A county-ArcGIS source in the app.** Parcels come from a county's ArcGIS feature service, and an imagery year comes from its ArcGIS ortho service. When the export endpoint fails, the service's cached tiles are used. Adding a county is configuration, not code. Peoria is the first county, and Richwoods Township the first area.
- **Single-year structure inventory runs.** A run type that looks at one imagery year, next to the existing base-versus-target runs, whose results are stored per parcel.
- **The vision model as the inventory's engine.** `qwen3-vl` through the user's model proxy, with a configurable endpoint, key and model name. The key never reaches the repository.
- **Reviewing inventories in the app.** The run list shows inventory runs. The parcel list filters by structure kind, and the viewer draws the model's structures on the image beside the county's record.
- **Measurement.** User labels from contact sheets for a Richwoods sample, and the model's accuracy by structure kind against them. Agreement with county records. Throughput and a county-scale projection. All recorded in `backend/eval/README.md`.
- **Imagery source comparison report.** The research below, published as a page for the user's review.

### Explicitly Out of Scope
- **Detecting new or changed structures.** 2015→2019 comparison is the next PRD, once the inventory is shown to be reliable.
- **Changing production runs or the default detector.** `segmenter-v1` remains production's default; this runs on the local stack only.
- **Deploying the vision model** to Fargate or AWS.
- **Paid imagery** (Nearmap, Vexcel, Hexagon, EagleView, satellite). Kept in the report for comparison only.
- **Counties without ArcGIS services**, and **Google, Bing or Esri basemaps**, whose terms forbid tracing buildings.
- **Confirm/dismiss in the app.** Labels come from contact sheets. In-app review decisions are Plan C.
- **Managing the model host.** `qwen3-vl` is already served; this work only calls it.

## Technical Context

- **Model endpoint (verified 2026-09-23):**
  - A LiteLLM proxy with an OpenAI-compatible API at `http://pge-hermes-00:4000/v1`, or `http://100.94.131.26:4000/v1` over Tailscale, backed by Ollama. The local worker must be able to reach it.
  - Access details are in the untracked `access-qwen.md` at the repo root. It contains the proxy key and must stay out of version control.
  - Vision models: `qwen3-vl`, and `vision`, which appears to be another name for the same model. Both answered an image request correctly.
  - On a real 2019 Peoria parcel image, `qwen3-vl` correctly described an empty graded lot, a small shed, and the neighbouring houses and driveways.
  - Latency: 45 s on the first call (model load), then about 7 s per image. That puts Richwoods' 3,242 parcels at about 6.3 hours per imagery year on one model instance. The other listed models are text-only.
- **Peoria County (verified 2026-09-23):**
  - **Parcels:** `https://services.arcgis.com/iPiPjILCMYxPZWTc/arcgis/rest/services/Tax_Parcels/FeatureServer/5` has 90,344 parcels. Its fields include `PIN`, `year_built`, `eff_year_built`, `total_living_area`, `gar_area`, `det_gar_area`, `improvements_value` and `PropClass`. The owner and address fields (`owner_name`, `ADDR1`, `prop_street` and others) must never reach a committed file.
  - **Richwoods:** the `POL_TWP_NAME = 'RICHWOODS'` features, 4 pieces, of `https://gis.peoriacounty.gov/arcgis/rest/services/DP/Peoria_Reference/MapServer/2` (Political Townships). They cover about 8.4 km² and intersect 3,242 parcels, 2,586 of them with a recorded build year.
  - **2015 imagery:** `https://gis.peoriacounty.gov/arcgis/rest/services/RL/Orthos2015/MapServer`, a Web Mercator (EPSG:3857) tile cache whose finest level is about 0.149 m.
  - Parcel image `export` worked on the 2019 service. On the 2024 service it returned blank while its cached tiles worked, so the 2015 service's behaviour must be confirmed.
  - The county's licence allows this work, per the user.
- **Existing code:**
  - Runs compare a base and a target year: `backend/src/ptax/detection/run.py` for the run job, `backend/src/ptax/api/runs.py` for the API, `run_parcels` for storage, and the Runs page and parcel viewer in `frontend/src/pages/`.
  - Parcel layers come in as uploaded files (`ptax.parcels`); imagery years come from NAIP or uploads (`ptax.imagery`).
  - The detector registry (`ptax.detection.registry`) already selects detectors by name.
  - The Hennepin labelling method is `ptax-eval chips` contact sheets plus a visual-labels JSON file (`backend/eval/`).
  - Nothing in the repository reads ArcGIS services as a source, runs a single-year inventory, or calls a vision model.

## Key Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| First step | Inventory existing structures on one image; no change detection | The user's choice. A model that can't list what is there will misreport what changed. |
| Imagery year | Peoria 2015 | The user's choice: the first year of the equal-quality 2015/2019 pair used next. |
| Area | Richwoods Township (political), 3,242 parcels | The user's choice; a manageable overnight scan. |
| Parcel and imagery source | The county's own ArcGIS services | The user's choice. Peoria's orthophotos are about 0.15 m, and many counties publish through ArcGIS. |
| Where it runs | Inside the ptax-finder app, run locally | The user's choice. Review uses the existing run list and parcel viewer. |
| Model | `qwen3-vl` through the user's proxy | Verified to read aerial imagery. |
| Ground truth | The user's contact-sheet labels, with county records as a secondary check | The same method the Hennepin gate used. County records miss sheds and pools. |

## Research Findings

Checked on 2026-09-23. Paid prices are quote-based unless stated.

**The main finding:** neither satellite nor aerial imagery includes parcel boundaries. Parcels always come from a separate dataset overlaid on the imagery. Counties on ArcGIS publish both through the same REST API, which is what this PRD uses.

### Imagery, free

| Source | Resolution | Coverage | Notes |
|---|---|---|---|
| **County ArcGIS orthophotos** (chosen) | varies by county; Peoria about 0.15 m tiles | each county's own flights (Peoria: 1939, 1997, 2003, 2008, 2011, 2015, 2019, 2024) | Free and published by the county; the same REST API everywhere. Quality and years differ by county. |
| USDA NAIP | 0.6 m (recent), 1.0 m (older) | all of the lower 48, every 2–3 years | What the project uses today; free and public domain. |
| USGS High Resolution Orthoimagery | 1 m or finer | patchwork of mostly metro areas and years | Free, not consistent. |
| Esri World Imagery / Wayback | varies | undated mosaic | Licence limits export; mixed sources. Unsuitable. |
| Google Maps / Earth satellite | varies | undated mosaic | **Excluded.** Terms forbid tracing or digitising building outlines from the satellite base map. |

### Imagery, paid (for comparison)

| Source | Resolution | Coverage | Pricing and access |
|---|---|---|---|
| Nearmap | 4.4–7.5 cm vertical, plus obliques | most of the US population, re-flown up to 3× a year | Annual subscription, quote |
| Vexcel Data Program | 15 cm across the lower 48 today; a 7.5 cm nationwide programme scheduled to start January 2027 | nationwide, plus 7.5 cm urban and archive | Quote |
| Hexagon HxGN Content Program | 30 cm US-wide, 15 cm in covered areas | contiguous US (per reseller) | Quote |
| EagleView (Reveal / Connect) | about 7.5–15 cm vertical, plus 45° obliques | claims 94% of the US population | Quote, with a property-data API |
| Vantor (formerly Maxar) WorldView | 15 cm HD, 30 cm, 50 cm satellite | nationwide, with a ~20-year archive | Per km², through resellers such as SkyFi |
| Airbus Pléiades Neo / Planet SkySat | 30 cm / 50 cm satellite | tasking | Per km², through resellers |

### Parcels and building footprints

| Source | What it gives | Cost |
|---|---|---|
| County ArcGIS parcel services (Peoria `Tax_Parcels`) | Exact boundaries and the county's building attributes | Free |
| Regrid | National standardised parcels, plus building footprints | Self-serve API (30-day sandbox); bulk quote-based |
| Microsoft / Overture building footprints | Building outlines only, undated | Free (ODbL) |

**Sources:**
- [Peoria County open data](https://data-peoriacountygis.opendata.arcgis.com/)
- [Peoria ortho services](https://gis.peoriacounty.gov/arcgis/rest/services/RL)
- [ISGS clearinghouse imagery](https://clearinghouse.isgs.illinois.edu/data/imagery)
- [USGS NAIP ImageServer](https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPImagery/ImageServer)
- [USGS HRO](https://www.usgs.gov/centers/eros/science/usgs-eros-archive-aerial-photography-high-resolution-orthoimagery-hro)
- [Google Maps Platform terms](https://cloud.google.com/maps-platform/terms)
- [Nearmap](https://www.nearmap.com/)
- [Vexcel 7.5 cm nationwide programme](https://vexceldata.com/stories/vexcel-announces-the-first-nationwide-u-s-aerial-imagery-program-at-7-5cm-resolution/)
- [HxGN Content Program](https://hxgncontent.com/en-us)
- [EagleView commercial imagery](https://company.eagleview.com/commercial-imagery/)
- [Vantor via SkyFi](https://skyfi.com/en/products/vantor)
- [Regrid plan options](https://support.regrid.com/reference/plan-options)
- [Qwen3-VL on Ollama](https://ollama.com/library/qwen3-vl:30b-a3b)

## Open Questions

1. **How many Richwoods parcels to label** for the measurement? Hennepin used 300; the sample should include enough garages, sheds and pools to measure each kind, not just houses.
2. **What accuracy counts as "reliable enough"** to move on to change detection? The plan should propose the bar, per structure kind, for the user to accept before the scan is judged.
3. **How precise the model's structure locations need to be.** Qwen3-VL can report where it sees things. Whether those positions are good enough to draw, or only the per-parcel list is trusted, is for the measurement to show.
