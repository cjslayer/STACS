"""Speckle quality metrics (deferred past v1).

Planned metrics computed on the reference frame, with a green/amber/red verdict:
  - mean intensity gradient (MIG)      -> overall texture / correlate-ability
  - speckle size via autocorrelation   -> dots should span a few pixels
  - contrast / histogram spread        -> dynamic range
  - % saturated pixels                 -> over/under-exposure
"""


def assess_speckle(image):
    """Return a dict of metrics + an overall verdict. TODO(deferred)."""
    raise NotImplementedError("deferred past v1")
