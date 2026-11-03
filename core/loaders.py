"""Loader framework: one class per source, idempotent, incremental.

A loader implements `load(full)` and returns how many rows it wrote or
updated. The framework records a LoaderRun around it and never lets an
exception escape past the run log. Loaders are registered by dotted path
in settings.LOADERS; `registry()` resolves them in that order.
"""

from __future__ import annotations

import logging
import traceback
from abc import ABC, abstractmethod
from importlib import import_module

from django.conf import settings
from django.utils import timezone

from core.models import LoaderRun, LoaderState

logger = logging.getLogger(__name__)


class Disabled(Exception):
    """Raised by a loader whose prerequisites are absent (no token, no
    binary). Recorded as ok with a message, not as a failure."""


class Loader(ABC):
    name: str  # short, stable; the key in LoaderRun and LoaderState
    cadence: str = "daily"  # "hourly" | "daily"; documentation for the scheduler

    @abstractmethod
    def load(self, full: bool = False) -> int:
        """Fetch and store. Return rows written or updated. Raise
        Disabled if the source isn't configured; raise anything else
        on failure."""

    # Convenience around LoaderState.
    def state_get(self, key: str, default: str | None = None) -> str | None:
        return LoaderState.get(self.name, key, default)

    def state_set(self, key: str, value: str) -> None:
        LoaderState.set(self.name, key, value)


def registry() -> dict[str, Loader]:
    loaders: dict[str, Loader] = {}
    for dotted in settings.LOADERS:
        module, _, cls = dotted.rpartition(".")
        loader = getattr(import_module(module), cls)()
        if loader.name in loaders:
            raise ValueError(f"duplicate loader name {loader.name!r}")
        loaders[loader.name] = loader
    return loaders


def run_loader(loader: Loader, full: bool = False) -> LoaderRun:
    run = LoaderRun.objects.create(loader=loader.name, started=timezone.now())
    try:
        rows = loader.load(full=full)
    except Disabled as exc:
        run.ok, run.message = True, f"disabled: {exc}"
    except Exception as exc:
        logger.exception("%s failed", loader.name)
        run.ok, run.message = False, f"{exc!r}\n{traceback.format_exc()[-2000:]}"
    else:
        run.ok, run.rows, run.message = True, rows, f"{rows} rows"
    run.finished = timezone.now()
    run.save()
    logger.info("%s: %s", loader.name, run.message.splitlines()[0])
    return run
