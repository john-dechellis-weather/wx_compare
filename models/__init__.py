"""Model registry."""
from .base import ModelSource
from .gfs_mos import GfsMos
from .hrrr import Hrrr, Station

ALL_MODEL_CLASSES = [GfsMos, Hrrr]

__all__ = ["ModelSource", "GfsMos", "Hrrr", "Station", "ALL_MODEL_CLASSES"]
