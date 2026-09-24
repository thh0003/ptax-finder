"""Reconciliation: Year A and Year B detections on a parcel -> changes and a parcel status.

Pure logic over geometries in the tenant's projected CRS; no I/O. `geometry` turns model
masks into detections on a parcel, `matching` pairs and classifies them, and `parcel`
combines the result with the change model into the parcel-level verdict.
"""
