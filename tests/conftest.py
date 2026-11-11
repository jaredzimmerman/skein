"""Shared fixtures. Other test modules may add their own below."""

from __future__ import annotations

import pytest

from skein.service import Skein
from skein.store import Store

T0 = 1_800_000_000.0  # a fixed, arbitrary start time (2027-01-15)


class FakeClock:
    """A clock tests can move. Call it like ``time.time``."""

    def __init__(self, start: float = T0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "skein.db"


@pytest.fixture
def store(db_path) -> Store:
    return Store(db_path)


@pytest.fixture
def skein(store, clock) -> Skein:
    return Skein(store, clock=clock)
