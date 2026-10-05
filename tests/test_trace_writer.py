from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path

import jsonschema
import pytest

from galois_edge.profile_schema import CommandConfig, ParameterConfig as P, ReturnConfig
from galois_edge.tracing import CommandContext, CommandEvent, TraceWriter, WriteTarget

pytestmark = pytest.mark.critical
SCHEMA = json.loads((Path(__file__).resolve().parents[1]
                     / "third_party/edgesim/contracts/schemas/trace-v1.schema.json").read_text())
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)
ADDR = "TCPIP0::10.0.0.5::5025::SOCKET"
SETV = CommandConfig(type="property", getter=":SOUR{channel}:VOLT?", setter=":SOUR{channel}:VOLT {voltage}",
                     params={"channel": P(type="int"), "voltage": P(type="float")},
                     returns=ReturnConfig(type="float"))
STATE = CommandConfig(type="property", getter=":OUTP{channel}?", setter=":OUTP{channel} {state}",
                      params={"channel": P(type="int"),
                              "state": P(type="enum", options=["ON", "OFF"], map={"ON": 1, "OFF": 0})})


def _ev(scpi="*RST", ctx=None, **kw):
    base = dict(instrument_id=ADDR, scpi=scpi, context=ctx, is_query=False, success=True, response=None,
                response_bytes=None, error="", t_wall_ns=1_000_500, latency_ns=1234)
    base.update(kw)
    return CommandEvent(**base)


def _lines(w):
    return [json.loads(line) for line in w.path.read_text().splitlines()]


@pytest.fixture
def writer(tmp_path):
    w = TraceWriter(tmp_path, run_id="run-1", clock_ns=lambda: 1_000_000, engine_version="0.1.0")
    w.start()
    yield w
    w.close()


def test_every_record_validates_against_the_contract_schema(writer):
    ctx = CommandContext(ADDR, "source.voltage", {"channel": 2, "voltage": 5.0}, SETV, False, "setter",
                         (WriteTarget("output.voltage_setpoint", "channel", 1, 2, "float"),))
    writer.observe(_ev(":SOUR2:VOLT 5", ctx))
    qctx = CommandContext(ADDR, "source.voltage", {"channel": 1}, SETV, True, "getter")
    writer.observe(_ev(":SOUR1:VOLT?", qctx, is_query=True, response="+5.000000E+00"))
    writer.observe(_ev(":WAV:DATA?", is_query=True, response_bytes=b"#14abcd\n"))
    writer.observe(_ev(":BAD", success=False, error="Command error: x"))
    writer.observe(_ev(":SLOW?", success=False, error="Timeout after 5000ms: t", timed_out=True))
    writer.close()
    recs = _lines(writer)
    for rec in recs:
        VALIDATOR.validate(rec)
    assert [r["kind"] for r in recs] == ["run_start"] + ["transition"] * 5 + ["run_end"]
    start, setr, getr, binr, err, slow, end = recs
    assert start["provenance"] == "real" and start["instruments"] == {} and start["engine"]["name"] == "galois-edge"
    assert setr["action"] == {"kind": "command", "raw": ":SOUR2:VOLT 5", "path": "source.voltage",
                              "params": {"channel": 2, "voltage": 5.0}}
    assert setr["delta"] == {"output.voltage_setpoint[2]": [None, 5.0]}
    assert (setr["seq"], setr["t_virtual_ns"], setr["fidelity"], setr["status"]) == (0, 500, "exact", "ok")
    assert getr["observation"]["typed"] == 5.0 and getr["observation"]["response"] == "+5.000000E+00"
    assert binr["action"]["kind"] == "scpi" and binr["observation"]["response"] is None
    assert binr["observation"]["data_ref"]["nbytes"] == 8
    assert (err["status"], slow["status"]) == ("error", "timeout")
    assert end["seq"] == 5 and end["status"] == "ok"


def test_blob_is_content_addressed(writer, tmp_path):
    writer.observe(_ev(":WAV?", is_query=True, response_bytes=b"payload"))
    writer.observe(_ev(":WAV?", is_query=True, response_bytes=b"payload"))
    ref = _lines(writer)[1]["observation"]["data_ref"]
    sha = hashlib.sha256(b"payload").hexdigest()
    assert ref == {"kind": "blob", "uri": f"blobs/{sha}.bin", "sha256": sha, "nbytes": 7}
    assert (tmp_path / "blobs" / f"{sha}.bin").read_bytes() == b"payload"


@pytest.mark.parametrize("target,params,expected", [
    (WriteTarget("output.enabled", "channel", 1, 2, "bool"), {"channel": 1, "state": "ON"},
     {"output.enabled[1]": [None, True]}),
    (WriteTarget("output.enabled", "channel", 1, 2, "bool"), {"state": "OFF"},
     {"output.enabled[1]": [None, False], "output.enabled[2]": [None, False]}),
    (WriteTarget("output.enabled[2]", None, None, None, "bool"), {"channel": 1, "state": "ON"},
     {"output.enabled[2]": [None, True]}),
    (WriteTarget("mode", None, None, None, "enum", ("CVoltage", "CCurrent")), {"state": "ON"}, {}),
])
def test_delta_binding_and_coercion(writer, target, params, expected):
    ctx = CommandContext(ADDR, "output.state", params, STATE, False, "setter", (target,))
    writer.observe(_ev(":OUTP1 1", ctx))
    assert _lines(writer)[1]["delta"] == expected


def test_non_finite_values_are_strings(writer):
    ctx = CommandContext(ADDR, "q", {}, CommandConfig(scpi=":Q?", returns=ReturnConfig(type="float")), True, "query")
    writer.observe(_ev(":Q?", ctx, is_query=True, response="nan"))
    assert _lines(writer)[1]["observation"]["typed"] == "nan"


def test_concurrent_observers_produce_unique_seq(writer):
    def burst():
        for _ in range(50):
            writer.observe(_ev())
    threads = [threading.Thread(target=burst) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    seqs = [r["seq"] for r in _lines(writer) if r["kind"] == "transition"]
    assert sorted(seqs) == list(range(400))


def test_write_failures_never_raise(writer, caplog):
    def boom(*_a):
        raise OSError("disk full")
    writer._fh.write = boom
    with caplog.at_level(logging.WARNING, logger="galois_edge.tracing"):
        writer.observe(_ev())
        writer.observe(_ev())
    assert sum("disk full" in r.getMessage() for r in caplog.records) == 1


def test_close_is_idempotent_and_ignores_later_events(writer):
    writer.close()
    writer.close()
    writer.observe(_ev())
    assert [r["kind"] for r in _lines(writer)] == ["run_start", "run_end"]


def test_unwritable_blob_dir_keeps_the_transition(writer, tmp_path, caplog):  # Review Focus 5
    (tmp_path / "blobs").write_text("not a directory")
    with caplog.at_level(logging.WARNING, logger="galois_edge.tracing"):
        writer.observe(_ev(":WAV?", is_query=True, response_bytes=b"payload"))
        writer.observe(_ev(":WAV?", is_query=True, response_bytes=b"payload"))
    recs = _lines(writer)[1:]
    for rec in recs:
        VALIDATOR.validate(rec)
    assert [r["seq"] for r in recs] == [0, 1]
    assert all(r["observation"]["response"] is None and "data_ref" not in r["observation"] for r in recs)
    assert sum(r.levelno == logging.WARNING for r in caplog.records) == 1
