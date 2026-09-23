"""Offline detector evaluation: labelled real parcels, real NAIP, measured metrics.

This package is development tooling, not part of the API or worker runtime. It exists so
every change to ``ClassicalDetector`` is measured against real imagery with real labels
instead of against the synthetic fixtures the detector was originally written to satisfy.

Entry point: ``ptax-eval`` (see ``ptax.eval.cli``).
"""
