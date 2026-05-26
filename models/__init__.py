"""Model registry."""
from .base import ModelSource
from .gfs_mos import GfsMos
from .hrrr import Hrrr, Station
from .nbm import Nbm

ALL_MODEL_CLASSES = [GfsMos, Hrrr, Nbm]

__all__ = ["ModelSource", "GfsMos", "Hrrr", "Nbm", "Station", "ALL_MODEL_CLASSES"]
