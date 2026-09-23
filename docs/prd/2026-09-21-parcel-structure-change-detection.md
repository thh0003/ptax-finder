# Parcel Structure Change Detection (ptax-finder)

Created: 2026-09-21
Author: tholmes4005@gmail.com
Agent: Claude Code
Category: Feature
Status: Final
Research: Standard

## Problem Statement

County assessor offices are required to keep property valuations current, but new taxable structures (houses, garages, barns, additions, outbuildings) routinely go unrecorded when owners build without permits or when permit data never reaches the assessor. Finding them today means either driving every road or paying an imagery vendor (Nearmap, EagleView) for a bundled change-detection product tied to that vendor's imagery subscription.

ptax-finder is a multi-county SaaS that lets an assessor's office compare aerial imagery of every parcel in the county across two years, automatically flags parcels that appear to have gained a structure, and gives staff a review queue to confirm or dismiss each candidate with an audit trail. Historic years come free from USDA NAIP (public domain, already on AWS); newer years come from whatever imagery the county already licenses, uploaded as GeoTIFF/COG. The result is a defensible list of parcels to reassess, exportable to the county's assessment system, without locking the county to a single imagery vendor.

## Core User Flows

### Flow 1: Operator provisions a county tenant

1. Operator (the SaaS owner) creates a county tenant: county name, state, FIPS code.
2. Operator creates the first admin user for that county and sends them their login.
3. County admin signs in and can invite further users with a role of **admin** or **reviewer**.

### Flow 2: County admin loads parcel boundaries

1. Admin uploads the county's parcel layer (shapefile bundle or GeoJSON) containing parcel geometry and the county's parcel identifier field.
2. System validates the file, reprojects as needed, and reports parcel count and any rows it could not load.
3. Admin selects which attribute is the parcel ID (used in the review queue and export).
4. Admin can re-upload a newer parcel layer later; existing review decisions are matched to parcels by parcel ID.

### Flow 3: Admin makes imagery years available

1. Admin opens the Imagery page and sees, per year, whether imagery is available for the county's footprint and where it came from (NAIP or upload).
2. For NAIP: system lists the NAIP years that cover the county on AWS Open Data; admin selects years to ingest and the system fetches them for the county footprint.
3. For newer years: admin uploads GeoTIFF/COG orthoimagery for a labeled year (and optional provider name); system validates coverage against the county footprint and reports gaps.
4. Each ingested year shows a status (queued, processing, ready, failed with reason).

### Flow 4: Admin runs a comparison

1. Admin picks a **base year** and a **target year** from the ready years and starts a run.
2. System processes every parcel: compares imagery inside the parcel boundary between the two years and produces, per parcel, a **change score** plus a **candidate** flag for parcels likely to have gained built-up area.
3. The run shows progress and, on completion, a summary: parcels processed, candidates flagged, parcels skipped (no imagery coverage in one or both years).
4. Admin can run additional comparisons (e.g., 2021 vs 2023 and 2023 vs 2025); each run is retained.

### Flow 5: Reviewer works the queue

1. Reviewer opens a run's review queue: a sortable, filterable list of candidate parcels with parcel ID, change score, review status, and before/after thumbnails.
2. Reviewer opens a parcel and sees the base-year and target-year imagery side by side (and a toggle/swipe view), clipped to the parcel with surrounding context and the parcel boundary drawn.
3. Reviewer marks the parcel **Confirmed** (new structure present) or **Dismissed** (false positive), optionally adds a note, and moves to the next candidate.
4. Every status change records who made it and when. Notes and status history are visible on the parcel.
5. Reviewer can also browse non-candidate parcels and confirm a structure the detector missed.

### Flow 6: Map view

1. Any user opens the map for a run: imagery for the base or target year as the basemap, parcel boundaries overlaid, candidates and confirmed parcels highlighted by status.
2. Clicking a parcel opens the same review panel as Flow 5.

### Flow 7: Export confirmed parcels

1. Admin or reviewer exports a run's results as CSV: parcel ID, status, change score, reviewer, review date, note. Filterable to confirmed only.
2. The file is intended for import into the county's CAMA/assessment system.

## Scope

### In Scope

- Multi-county tenancy: each county's parcels, imagery, runs, and reviews are isolated; users belong to exactly one county.
- Authentication with two roles: **admin** (tenant settings, uploads, runs, users) and **reviewer** (queue, map, export). Operator-level tenant provisioning.
- Parcel boundary ingestion from shapefile or GeoJSON, with parcel-ID field selection and re-upload.
- NAIP imagery discovery and ingestion from AWS Open Data for the county footprint, by year.
- Imagery upload for any year as GeoTIFF/COG, with coverage validation against the county footprint.
- Comparison runs between a base year and a target year producing per-parcel change score and candidate flag; multiple retained runs per county.
- Review queue with sorting/filtering, side-by-side and swipe/toggle imagery per parcel, confirm/dismiss, notes, and a per-parcel audit trail (who, what, when). Any parcel in a run, candidate or not, can be opened and reviewed (from the queue's filters or the map).
- Map view with year imagery, parcel overlay, and status highlighting.
- CSV export of run results, filterable to confirmed parcels.
- AWS deployment defined in CDK: containers on ECS Fargate behind an Application Load Balancer, Aurora Serverless database, React + Vite frontend.

### Explicitly Out of Scope

- Self-serve signup and billing — tenants are provisioned by the operator in v1; commercial packaging is undecided.
- Direct provider API adapters (Nearmap, EagleView, Vexcel/Hexagon, Vantor, Planet) — every provider is contract-gated; v1 accepts uploaded orthos from any of them. The imagery-source boundary must leave room for adapters later.
- Live sync with county ArcGIS feature services or CAMA systems — file upload in and CSV out cover the v1 workflow.
- Oblique imagery, 3D, elevation/DSM — detection is on vertical ortho imagery only.
- Detecting demolitions, pools, solar panels, or other feature types — v1 flags gained built-up area only.
- Mobile app — desktop browser use by office staff.
- Public or taxpayer-facing portal — internal county tool only.
- Automatic valuation or reassessment notices — the output is a reviewed parcel list, not a dollar figure.

## Technical Context

- **Hosting (user-supplied NFR):** AWS; containers on ECS Fargate fronted by an Application Load Balancer; Aurora Serverless as the database; React + Vite frontend; all infrastructure via CDK.
- **Backend language:** Python (user delegated the Node/Python choice; see Key Decisions).
- **Imagery source facts that constrain the work:**
  - NAIP on AWS Open Data: `naip-visualization` bucket holds 3-band RGB Cloud Optimized GeoTIFFs; `naip-analytic` holds 4-band (RGB+NIR) MRF; `naip-source` holds raw 4-band GeoTIFF. Resolution 30–100 cm, "leaf-on" growing-season captures. Catalog covers 2010–2023 for most products; each state is refreshed on a 2–3 year cycle, so the newest NAIP year varies by state and lags the calendar year. Public domain with attribution.
  - Uploaded imagery will arrive at varied resolutions (7 cm aerial to 50 cm satellite), projections, and band counts. Sub-metre resolution is the practical floor for seeing small structures; 3 m-class satellite imagery (PlanetScope) is not usable for this purpose.
- **Data ownership:** parcel geometry, parcel IDs, review decisions, and notes are county data and must stay isolated per tenant. Review decisions must be attributable to a user for defensibility if a reassessment is challenged.
- **Scale envelope:** a county typically has tens of thousands to a few hundred thousand parcels; a full-county NAIP year is tens of GB. Comparison runs are long-running background work, not request/response.
- **Existing code:** none — the repository contains only `create_spec.md`.

## Key Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Historic imagery source | USDA NAIP from AWS Open Data | Free, public domain, already on S3 in the same cloud; covers 2010–2023 for most states. |
| Newer-than-NAIP imagery | County uploads GeoTIFF/COG from imagery they already license | All commercial providers are contract-gated APIs; upload works with any of them and keeps the product vendor-neutral. Adapter path kept open. |
| Detection model | Automated candidate flagging + mandatory human review | Reassessment has legal weight; a human confirms every parcel that will be acted on. Automation exists to cut the workload, not to make the decision. |
| Deployment shape | Multi-county SaaS, operator-provisioned tenants | User's choice; billing/self-serve deferred to keep v1 focused on the assessment workflow. |
| Parcel boundaries | Uploaded shapefile/GeoJSON with user-selected parcel-ID field | Counties own and maintain their parcel layers; upload is universal, ArcGIS sync is deferred. |
| Backend language | Python | User delegated the choice; the geospatial and imagery ecosystem (GDAL/rasterio, shapely, ML tooling) is Python-first. |
| Outputs | In-app review queue, map view, notes/audit trail, CSV export | All four selected by the user; CSV is the lowest-friction path into existing CAMA systems. |
| Feature type | New structures / gained built-up area only | Keeps the detection problem narrow and the review queue meaningful for v1. |

## Research Findings

Standard-tier research on imagery sources for years newer than NAIP (2026-09-21):

- **NAIP on AWS** — public domain, 30–100 cm, 2010–2023, state-by-state 2–3 year refresh. Source: https://registry.opendata.aws/naip/
- **Nearmap** — ~7 cm aerial, multiple captures per year in urban areas; developer APIs (Tile, Coverage, AI Feature, WMS). Markets "AI change detection for property assessment" directly to assessors, so it is both a potential source and the incumbent competitor. Sources: https://developer.nearmap.com/docs/coverage-api, https://www.nearmap.com/solutions/property-assessment
- **EagleView** — ~7–10 cm aerial ortho and oblique; Imagery API. Long-standing vendor to US county assessors. Source: https://developer.eagleview.com/documentation/imagery
- **Vexcel / Hexagon HxGN Content Program** — ~15–30 cm nationwide aerial program; streaming subscription or per-pixel download via HxDR. Vendor-neutral aerial alternative. Sources: https://hexagon.com/products/product-groups/geospatial-content/hxgn-content-program, https://vexceldata.com/
- **Vantor (formerly Maxar)** — 15–50 cm satellite, Vivid 30 cm mosaics, 20+ year archive; Vantor Hub API or resellers (SkyWatch, SkyFi) with per-km² pricing. Only satellite option sharp enough for small structures. Sources: https://vantor.com/product/platform/hub/, https://skywatch.com/geospatial-data-providers/vantor/
- **Planet** — PlanetScope 3 m daily monitoring (too coarse for this use), SkySat ~50 cm via tasking; credit-based pricing with sales-led contracts. Source: https://www.planet.com/pricing/insights-platform/

Trade-off that shaped scope: no provider offers self-serve API access suitable for building and testing v1 without a contract, while counties commonly already license one of them. Upload-based ingestion delivers vendor neutrality now and defers the adapter question until a customer's provider is known.
