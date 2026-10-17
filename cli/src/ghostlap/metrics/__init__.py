"""Metric registry. Importing this package registers every built-in metric."""

from . import combat, jump, motion, movement  # noqa: F401  (registration side effects)
from .base import REGISTRY, MetricDef, MetricValue, ProbeData, load_probe_data  # noqa: F401
