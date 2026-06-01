"""Model registry."""
from .base import ModelSource
from .gfs_mos import GfsMos
from .gfs_lamp import GfsLamp
from .hrrr import Hrrr, Station
from .nbm import Nbm
from .tomorrow_io import TomorrowIO

ALL_MODEL_CLASSES = [GfsMos, GfsLamp, Hrrr, Nbm, TomorrowIO]

__all__ = ["ModelSource", "GfsMos", "GfsLamp", "Hrrr", "Nbm", "TomorrowIO", "Station", "ALL_MODEL_CLASSES"]
