# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Static config-validation unit tests (no process spawned)."""

from pathlib import Path

import pytest

from mcp_forwarding_tester.config import (
    ConfigError,
    load_config,
    parse_config_obj,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_good_config_parses():
    cfg = load_config(FIXTURES / "good_config.json")
    names = {s.name for s in cfg.servers}
    assert names == {"filesystem", "weather"}
    fs = next(s for s in cfg.servers if s.name == "filesystem")
    assert fs.command == "/usr/local/bin/mcp-server-filesystem"
    assert fs.args == ["--root", "/home/appuser"]
    weather = next(s for s in cfg.servers if s.name == "weather")
    assert weather.args == []
    assert fs.ignored_keys == []


def test_ignored_keys_detected():
    doc = {
        "mcpServers": {
            "s": {"command": "/bin/true", "env": {"X": "1"}, "cwd": "/tmp"}
        }
    }
    cfg = parse_config_obj(doc, source="<test>")
    spec = cfg.servers[0]
    assert set(spec.ignored_keys) == {"env", "cwd"}
    assert spec.command == "/bin/true"


def test_missing_top_level_key():
    with pytest.raises(ConfigError):
        parse_config_obj({"servers": {}}, source="<test>")


def test_missing_command():
    with pytest.raises(ConfigError):
        parse_config_obj({"mcpServers": {"s": {"args": ["x"]}}}, source="<test>")


def test_bad_args_type():
    with pytest.raises(ConfigError):
        parse_config_obj(
            {"mcpServers": {"s": {"command": "/bin/true", "args": "x"}}},
            source="<test>",
        )


def test_non_object_root():
    with pytest.raises(ConfigError):
        parse_config_obj([], source="<test>")


def test_invalid_json_file(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with pytest.raises(ConfigError):
        load_config(p)


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.json")


def test_utf8_bom_is_detected_not_fatal(tmp_path):
    # A UTF-8 BOM must NOT crash the loader: the service rejects it, but the
    # tester strips it so it can still run the rest of the checks. The BOM is recorded
    # for the report.
    p = tmp_path / "bom.json"
    p.write_text('{"mcpServers": {"s": {"command": "/bin/true"}}}', encoding="utf-8-sig")
    cfg = load_config(p)
    assert cfg.bom == "UTF-8"
    assert [s.name for s in cfg.servers] == ["s"]


def test_no_bom_reports_none(tmp_path):
    p = tmp_path / "clean.json"
    p.write_text('{"mcpServers": {"s": {"command": "/bin/true"}}}', encoding="utf-8")
    cfg = load_config(p)
    assert cfg.bom is None


def test_bom_check_fails(tmp_path):
    # The config.encoding check should FAIL when a BOM is present.
    from mcp_forwarding_tester.checks import Level, check_config_file

    p = tmp_path / "bom.json"
    p.write_text('{"mcpServers": {"s": {"command": "/bin/true"}}}', encoding="utf-8-sig")
    cfg = load_config(p)
    results = check_config_file(cfg)
    enc = next(c for c in results if c.id == "config.encoding")
    assert enc.level is Level.FAIL
