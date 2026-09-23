"""The learned building segmenter: torch-only, installed by the optional ``ml`` group.

Nothing outside this package imports it at module level. `ptax.detection.registry` reaches
it only when the ``segmentation`` detector is chosen, so the API, the worker and the
classical evaluation path never need torch -- and the production image does not have it.
"""
