"""F1: CapabilityManager is read on the event loop while discovery registers on the I/O thread.

The I/O executor's discovery (main.py ``_discover_and_match``) calls ``register_instrument``
while MCP ``list_instruments`` / gRPC ``ListInstruments`` iterate the registry on the event
loop. Every collection the manager exposes or iterates must be a snapshot, or the reader fails
with "dictionary changed size during iteration".

The interleaving is forced, not hoped for: the first read of a profile attribute during the
call under test runs ``register_instrument`` on a worker thread and waits for it to finish, which
is exactly what the I/O thread does between two steps of the event loop's iteration.
"""
from __future__ import annotations

import sys
import threading
from types import SimpleNamespace
from typing import Any, Callable, Dict

import pytest

from galois_edge.capability_manager import CapabilityManager

pytestmark = pytest.mark.critical

_JOIN_S = 5.0   # bound on a worker that must not block; never a timing assumption


class _RegisterFromWorkerOnce:
    """Armed: the first call registers ``LATE`` from a worker thread and waits for it.

    The worker must finish: an accessor that held the registry lock while calling into
    caps/profile code would deadlock the I/O thread, which the join bound turns into a failure.
    """

    def __init__(self, mgr: CapabilityManager) -> None:
        self._mgr = mgr
        self.armed = False
        self.fired = False

    def __call__(self) -> None:
        if not self.armed or self.fired:
            return
        self.fired = True
        worker = threading.Thread(target=self._mgr.register_instrument, args=("LATE", "LATE"),
                                  name="instrument-io-test")
        worker.start()
        worker.join(timeout=_JOIN_S)
        assert not worker.is_alive(), "register_instrument blocked while a reader iterated"


class _Profile:
    """The profile surface InstrumentCapabilities reads; every read runs ``on_read`` first."""

    def __init__(self, key: str, on_read: Callable[[], None]) -> None:
        self._key = key
        self._on_read = on_read
        self._commands = {"MEAS": SimpleNamespace(enabled=True)}
        self._sequences = {"warmup": SimpleNamespace(enabled=True)}

    @property
    def profile_key(self) -> str:
        self._on_read()
        return self._key

    @property
    def instrument(self) -> Any:
        self._on_read()
        return SimpleNamespace(manufacturer="ACME", model=self._key, instrument_class="dmm")

    @property
    def commands(self) -> Dict[str, Any]:
        self._on_read()
        return self._commands

    @property
    def sequences(self) -> Dict[str, Any]:
        self._on_read()
        return self._sequences

    def path_of(self, name: str) -> str:
        self._on_read()
        if name in self._commands:
            return name
        raise KeyError(name)

    def get_sequence(self, name: str) -> Any:
        return self._sequences.get(name)

    def to_capability_dict(self) -> Dict[str, Any]:
        self._on_read()
        return {"has_profile": True, "profile_key": self._key, "manufacturer": "ACME",
                "model": self._key, "instrument_class": "dmm",
                "commands": [{"name": n} for n in self._commands],
                "sequences": [{"name": n} for n in self._sequences], "settings": {}}


def _manager_with_three_instruments():
    mgr = CapabilityManager()
    trigger = _RegisterFromWorkerOnce(mgr)
    for i in range(3):
        mgr.register_instrument(f"I{i}", f"I{i}", f"ACME,M{i},0,1", _Profile(f"acme_m{i}", trigger))
    trigger.armed = True
    return mgr, trigger


_READERS = {
    "all_instruments.items": lambda m: [(i, c.profile_key) for i, c in m.all_instruments.items()],
    "all_instruments.values": lambda m: [c.profile_key for c in m.all_instruments.values()],
    "all_instruments.keys": lambda m: [m.get_instrument_caps(i).profile_key for i in m.all_instruments],
    "find_by_class": lambda m: m.find_by_class("dmm"),
    "find_with_command": lambda m: m.find_with_command("MEAS"),
    "find_with_sequence": lambda m: m.find_with_sequence("warmup"),
    "get_all_capabilities": lambda m: m.get_all_capabilities(),
    "get_all_capabilities_list": lambda m: m.get_all_capabilities_list(),
    "get_available_classes": lambda m: m.get_available_classes(),
    "get_available_commands": lambda m: m.get_available_commands(),
    "get_summary": lambda m: m.get_summary(),
}


@pytest.mark.parametrize("reader", sorted(_READERS))
def test_a_registration_on_the_io_thread_mid_read_does_not_break_the_reader(reader):
    mgr, trigger = _manager_with_three_instruments()

    _READERS[reader](mgr)   # old code: RuntimeError: dictionary changed size during iteration

    assert trigger.fired
    assert "LATE" in mgr.all_instruments


def test_all_instruments_is_an_immutable_snapshot():
    mgr, _ = _manager_with_three_instruments()
    snapshot = mgr.all_instruments

    mgr.register_instrument("NEW", "NEW")
    mgr.unregister_instrument("I0")

    assert sorted(snapshot) == ["I0", "I1", "I2"]          # taken before the mutation
    assert sorted(mgr.all_instruments) == ["I1", "I2", "NEW"]
    with pytest.raises(TypeError):
        snapshot["X"] = snapshot["I1"]                      # a write to a copy would be silently lost


def test_readers_survive_register_and_unregister_churn_from_another_thread():
    """Stress: a worker churns the registry while this thread reads every collection accessor."""
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)   # switch threads as often as possible; restored below
    try:
        mgr = CapabilityManager()
        for i in range(8):
            mgr.register_instrument(f"S{i}", f"S{i}", f"ACME,M{i},0,1")
        reading, done = threading.Event(), threading.Event()
        errors: list = []

        def churn() -> None:
            try:
                reading.wait(timeout=_JOIN_S)   # churn while this thread reads, not before it starts
                for n in range(400):
                    mgr.register_instrument(f"C{n}", f"C{n}")
                    if n:
                        mgr.unregister_instrument(f"C{n - 1}")
            except BaseException as exc:   # pragma: no cover - surfaced by the assert below
                errors.append(exc)
            finally:
                done.set()

        worker = threading.Thread(target=churn, name="instrument-io-test")
        worker.start()
        reading.set()
        reads = 0
        while not done.is_set() or reads == 0:   # at least one full pass, however the threads are scheduled
            for _, caps in mgr.all_instruments.items():
                caps.manufacturer
            mgr.profiled_count
            mgr.instrument_count
            mgr.find_by_class("")
            mgr.get_all_capabilities_list()
            mgr.get_summary()
            reads += 1
        worker.join(timeout=_JOIN_S)
    finally:
        sys.setswitchinterval(previous)

    assert not worker.is_alive()
    assert errors == []
