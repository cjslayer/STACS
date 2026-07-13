"""CLI harness tests: argument wiring, setvar type-checking, and an
end-to-end subcommand run against the fake logger (no hardware)."""

import pytest

import stac_cli
from pakbus.errors import ValueTypeError
from pakbus.tabledefs import TYPE_BY_NAME, FieldDef
from tests.test_client import (CollectingLogger, FakeTransport,
                               basic_responder)
from pakbus.client import PakBusClient


def _field(type_name, dimension=1, read_only=False):
    return FieldDef("X", TYPE_BY_NAME[type_name].code, read_only,
                    "Smp", "", "", 1, dimension)


# --------------------------------------------------------- setvar type checks

def test_wire_type_float_fields():
    for tname in ("FP2", "IEEE4B", "IEEE8B"):
        wire, value = stac_cli._wire_type_for(_field(tname), "1.5")
        assert value == 1.5
    assert stac_cli._wire_type_for(_field("IEEE8B"), "1.5")[0] == "IEEE8B"
    assert stac_cli._wire_type_for(_field("FP2"), "2")[0] == "IEEE4B"
    with pytest.raises(ValueTypeError):
        stac_cli._wire_type_for(_field("IEEE4B"), "not-a-number")


def test_wire_type_integer_fields_range_checked():
    wire, value = stac_cli._wire_type_for(_field("Int2"), "-100")
    assert (wire, value) == ("Int4", -100)
    wire, value = stac_cli._wire_type_for(_field("UInt2"), "65535")
    assert (wire, value) == ("UInt4", 65535)
    with pytest.raises(ValueTypeError):
        stac_cli._wire_type_for(_field("UInt2"), "65536")   # out of range
    with pytest.raises(ValueTypeError):
        stac_cli._wire_type_for(_field("Int1"), "1.5")      # not an integer
    with pytest.raises(ValueTypeError):
        stac_cli._wire_type_for(_field("UInt4"), "-1")


def test_wire_type_bool_fields():
    assert stac_cli._wire_type_for(_field("Bool"), "true") == ("Bool", True)
    assert stac_cli._wire_type_for(_field("Bool"), "0") == ("Bool", False)
    with pytest.raises(ValueTypeError):
        stac_cli._wire_type_for(_field("Bool"), "maybe")


def test_wire_type_string_fields_length_checked():
    field = _field("ASCII", dimension=8)
    assert stac_cli._wire_type_for(field, "short") == ("ASCII", "short")
    with pytest.raises(ValueTypeError):
        stac_cli._wire_type_for(field, "waaaay too long for eight")


# ------------------------------------------------------------------ parsing

def test_parser_requires_subcommand():
    with pytest.raises(SystemExit):
        stac_cli.build_parser().parse_args([])


def test_parser_monitor_defaults():
    args = stac_cli.build_parser().parse_args(
        ["--port", "COM7", "monitor", "Fast"])
    assert args.interval == 0.25
    assert args.table == "Fast"
    assert args.my_addr == 4093        # STAC default, not LoggerNet's 4094
    assert args.logger_addr == 1


def test_sendfile_refuses_without_wipe_flag(tmp_path, monkeypatch, capsys):
    program = tmp_path / "test.CR6"
    program.write_text("Public x\n")
    monkeypatch.setattr(stac_cli, "make_client",
                        lambda args: (_ for _ in ()).throw(
                            AssertionError("must not even connect")))
    # make_client is called before the handler; patch it to a harmless stub
    monkeypatch.setattr(
        stac_cli, "make_client",
        lambda args: PakBusClient(FakeTransport(basic_responder()),
                                  my_addr=4093, logger_addr=1))
    rc = stac_cli.main(["--port", "COM7", "sendfile", str(program)])
    assert rc == 1
    assert "i-understand-this-wipes-tables" in capsys.readouterr().out


# ------------------------------------------------------------- end to end

def test_cli_collect_end_to_end(monkeypatch, capsys):
    """Full path: argv -> parser -> connect -> collect -> printed record."""
    responder = CollectingLogger()

    def fake_make_client(args):
        assert args.port == "COM7"
        return PakBusClient(FakeTransport(basic_responder(responder)),
                            my_addr=args.my_addr,
                            logger_addr=args.logger_addr, timeout=0.15)

    monkeypatch.setattr(stac_cli, "make_client", fake_make_client)
    rc = stac_cli.main(["--port", "COM7", "collect", "Fast", "--count", "2"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Fast #99" in out
    assert "Fast #100" in out
    assert "Load=100" in out


def test_cli_ping_end_to_end(monkeypatch, capsys):
    monkeypatch.setattr(
        stac_cli, "make_client",
        lambda args: PakBusClient(FakeTransport(basic_responder()),
                                  my_addr=args.my_addr,
                                  logger_addr=args.logger_addr,
                                  timeout=0.15))
    rc = stac_cli.main(["--port", "COM7", "ping"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "round-trip time" in out
    assert "neighbor established" in out
