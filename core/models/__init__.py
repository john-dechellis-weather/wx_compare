"""Model registry. The notebook should import from here.

To add a new model:
  1. Implement models/<name>.py subclassing ModelSource
  2. Add it to ALL_MODEL_CLASSES below
  3. Done — compare.run_comparison() will pick it up automatically.
"""
from .base import ModelSource
from .gfs_mos import GfsMos
from .hrrr import Hrrr, Station

ALL_MODEL_CLASSES: list[type[ModelSource]] = [GfsMos, Hrrr]

__all__ = ["ModelSource", "GfsMos", "Hrrr", "Station", "ALL_MODEL_CLASSES"]
