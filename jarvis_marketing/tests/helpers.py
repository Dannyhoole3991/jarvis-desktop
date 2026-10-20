"""Shared test helpers: a fresh store in a temp folder and a controllable clock."""

import os
import tempfile

from jarvis_marketing import config
from jarvis_marketing.store import Store


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def temp_store(clock=None):
    folder = tempfile.mkdtemp(prefix="jm-test-")
    return Store(os.path.join(folder, "m.db"), clock=clock or Clock()), folder


def business(**over):
    b = config.business(path=os.path.join(tempfile.mkdtemp(), "none.json"))
    b.update(over)
    return b
