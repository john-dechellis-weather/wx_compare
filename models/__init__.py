"""Model registry."""
from .base import ModelSource
from .gfs_mos import GfsMos
from .gfs_lamp import GfsLamp
from .hrrr import Hrrr, Station
from .nbm import Nbm

ALL_MODEL_CLASSES = [GfsMos, GfsLamp, Hrrr, Nbm]

__all__ = ["ModelSource", "GfsMos", "GfsLamp", "Hrrr", "Nbm", "Station", "ALL_MODEL_CLASSES"]
