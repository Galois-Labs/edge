#!/usr/bin/env python3
"""
Convert Labber INI instrument drivers to Galois YAML profiles.

Usage:
    python scripts/labber2galois.py /path/to/labber-drivers/ src/galois_edge/profiles/

Reads each Labber driver directory, parses the INI file, and writes a
corresponding YAML profile compatible with galois-edge's profile_loader.

MIT-licensed Labber drivers (c) 2015 Lab Control Software Scandinavia AB.
"""

from __future__ import annotations

import configparser
import os
import re
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Labber INI parser (handles their non-standard format)
# ---------------------------------------------------------------------------

def parse_labber_ini(path: str) -> dict[str, dict[str, str]]:
    """Parse a Labber INI file into {section: {key: value}} dict.

    Labber INI files use both ':' and '=' as delimiters and have
    section names that contain special characters. We parse manually
    rather than relying on configparser's strictness.
    """
    sections: dict[str, dict[str, str]] = {}
    current_section: str | None = None

    with open(path, encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            line = raw_line.strip()

            # Skip blank lines and comments
            if not line or line.startswith("#") or line.startswith(";"):
                continue

            # Section header
            if line.startswith("[") and line.endswith("]"):
                current_section = line[1:-1].strip()
                if current_section not in sections:
                    sections[current_section] = {}
                continue

            if current_section is None:
                continue

            # Key-value: split on first '=' or ':'
            for sep in ("=", ":"):
                idx = line.find(sep)
                if idx > 0:
                    key = line[:idx].strip().lower()
                    val = line[idx + 1:].strip()
                    sections[current_section][key] = val
                    break

    return sections


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------

def extract_instrument_meta(sections: dict) -> dict[str, Any]:
    """Extract instrument metadata from General settings and Model sections."""
    general = sections.get("General settings", {})
    model_sec = sections.get("Model and options", {})

    name = general.get("name", "Unknown Instrument")
    parts = name.split(" ", 1)

    # Try to extract manufacturer and model from the name
    manufacturer = parts[0] if parts else "Unknown"
    model = parts[1] if len(parts) > 1 else name

    # Clean up common patterns
    manufacturer = manufacturer.replace("_", " ")

    # Extract model strings for identity pattern
    model_ids = []
    for i in range(1, 20):
        mid = model_sec.get(f"model_id_{i}", "")
        if mid:
            model_ids.append(mid)
        mstr = model_sec.get(f"model_str_{i}", "")
        if mstr and mstr not in model_ids:
            model_ids.append(mstr)

    return {
        "name": name,
        "manufacturer": manufacturer,
        "model": model,
        "model_ids": model_ids,
    }


def extract_visa_settings(sections: dict) -> dict[str, Any]:
    """Extract VISA/communication settings."""
    visa = sections.get("VISA settings", {})
    settings: dict[str, Any] = {}

    timeout = visa.get("timeout", "5")
    try:
        settings["timeout_ms"] = int(float(timeout) * 1000)
    except ValueError:
        settings["timeout_ms"] = 5000

    term = visa.get("term_char", "")
    if "CR+LF" in term:
        settings["terminator"] = "\\r\\n"
    elif "CR" in term:
        settings["terminator"] = "\\r"
    else:
        settings["terminator"] = "\\n"

    settings["use_visa"] = visa.get("use_visa", "True").lower() == "true"
    settings["init_cmd"] = visa.get("init", "")
    settings["final_cmd"] = visa.get("final", "")
    settings["error_cmd"] = visa.get("error_cmd", "")

    return settings


# ---------------------------------------------------------------------------
# Instrument class inference
# ---------------------------------------------------------------------------

INSTRUMENT_CLASSES = {
    "sourcemeter": ["smu", "source", "sourcemeter"],
    "smu": ["smu", "source meter"],
    "multimeter": ["multimeter", "dmm", "voltmeter"],
    "dmm": ["multimeter", "dmm", "voltmeter", "3478", "34401", "3458"],
    "oscilloscope": ["oscilloscope", "scope", "dso"],
    "spectrum_analyzer": ["spectrum", "analyzer", "sa mode", "mxa"],
    "network_analyzer": ["network analyzer", "vna"],
    "signal_generator": ["signal generator", "waveform", "awg", "synthesizer", "rf source"],
    "awg": ["awg", "arbitrary waveform"],
    "lockin": ["lock-in", "lockin", "lock in"],
    "power_supply": ["power supply", "dc source", "voltage source", "gs200", "gs210", "7651", "6632"],
    "counter": ["counter", "5313"],
    "digitizer": ["digitizer", "acqiris", "alazar"],
    "attenuator": ["attenuator", "vaunix"],
    "switch": ["switch", "mini-circuits"],
    "temperature_controller": ["temperature", "lakeshore", "33x", "37x", "475", "cryomagnetics"],
    "magnet_controller": ["magnet", "ips", "mercury"],
    "cryostat": ["triton", "dilution", "bluefors", "cryostat", "fridge"],
    "spectrometer": ["spectrometer", "ocean optics"],
    "motion_controller": ["motion", "newport", "mm4006"],
    "daq": ["daq", "ni_daq", "ni_usb", "data acquisition"],
    "level_meter": ["level", "lm510", "ilm"],
    "delay_generator": ["delay", "dg645", "ds345", "645"],
}


def infer_class(name: str) -> str:
    """Infer instrument class from the driver name."""
    name_lower = name.lower()
    for cls, keywords in INSTRUMENT_CLASSES.items():
        for kw in keywords:
            if kw in name_lower:
                return cls
    return "instrument"


# ---------------------------------------------------------------------------
# Command conversion
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    """Convert a Labber section name to a YAML-safe command key."""
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = s.strip("_")
    return s


def convert_quantity(name: str, fields: dict[str, str]) -> dict[str, Any] | None:
    """Convert a Labber quantity section to a Galois command dict."""
    datatype = fields.get("datatype", "").upper()
    if not datatype:
        return None

    set_cmd = fields.get("set_cmd", "")
    get_cmd = fields.get("get_cmd", "")
    permission = fields.get("permission", "BOTH").upper()
    unit = fields.get("unit", "")
    description = name  # Use the section name as description

    # Skip non-SCPI commands (custom Python)
    if set_cmd == "<python>" or get_cmd == "<python>":
        # Still include with the raw command if there's a fallback
        pass

    cmd: dict[str, Any] = {}
    cmd["description"] = description

    # Determine type based on permission and available commands
    if permission == "READ":
        effective_get = get_cmd or (set_cmd + "?" if set_cmd else "")
        if not effective_get:
            return None
        cmd["scpi"] = effective_get
        cmd["type"] = "query"
        _add_returns(cmd, datatype, unit, fields)
    elif permission == "WRITE":
        if not set_cmd:
            return None
        cmd["type"] = "write"
        if datatype == "COMBO":
            _handle_combo_write(cmd, set_cmd, fields)
        elif datatype == "BUTTON":
            cmd["scpi"] = set_cmd
        else:
            cmd["scpi"] = _format_set_cmd(set_cmd)
            _add_params(cmd, datatype, unit, fields)
    else:
        # BOTH -> property with getter/setter
        if set_cmd or get_cmd:
            effective_get = get_cmd or (set_cmd + "?" if set_cmd else "")
            if datatype == "COMBO":
                _handle_combo_property(cmd, set_cmd, effective_get, fields)
            elif datatype == "BUTTON":
                cmd["scpi"] = set_cmd
                cmd["type"] = "write"
            else:
                if set_cmd and effective_get:
                    cmd["getter"] = effective_get
                    cmd["setter"] = _format_set_cmd(set_cmd)
                    cmd["type"] = "property"
                    _add_params(cmd, datatype, unit, fields)
                    _add_returns(cmd, datatype, unit, fields)
                elif effective_get:
                    cmd["scpi"] = effective_get
                    cmd["type"] = "query"
                    _add_returns(cmd, datatype, unit, fields)
                elif set_cmd:
                    cmd["scpi"] = _format_set_cmd(set_cmd)
                    cmd["type"] = "write"
                    _add_params(cmd, datatype, unit, fields)
                else:
                    return None
        else:
            return None

    return cmd


def _format_set_cmd(cmd: str) -> str:
    """Replace Labber's <*> placeholder with {value}."""
    if "<*>" in cmd:
        return cmd.replace("<*>", "{value}")
    if "<python>" in cmd:
        return cmd.replace("<python>", "{value}")
    # If no placeholder, append {value}
    return f"{cmd} {{value}}"


def _add_params(cmd: dict, datatype: str, unit: str, fields: dict) -> None:
    """Add parameter definitions based on datatype."""
    param: dict[str, Any] = {}

    if datatype == "DOUBLE":
        param["type"] = "float"
    elif datatype == "BOOLEAN":
        param["type"] = "enum"
        param["options"] = ["ON", "OFF"]
    elif datatype == "STRING":
        param["type"] = "string"
    elif datatype in ("COMPLEX", "VECTOR", "VECTOR_COMPLEX"):
        param["type"] = "string"
    else:
        param["type"] = "float"

    if unit:
        param["unit"] = unit

    low = fields.get("low_lim", "")
    high = fields.get("high_lim", "")
    if low and low not in ("-INF", "-inf"):
        try:
            param["min"] = float(low)
        except ValueError:
            pass
    if high and high not in ("+INF", "INF", "inf", "+inf"):
        try:
            param["max"] = float(high)
        except ValueError:
            pass

    if param:
        cmd["params"] = {"value": param}


def _add_returns(cmd: dict, datatype: str, unit: str, fields: dict) -> None:
    """Add return type information."""
    ret: dict[str, Any] = {}
    if datatype == "DOUBLE":
        ret["type"] = "float"
    elif datatype == "BOOLEAN":
        ret["type"] = "bool"
    elif datatype == "STRING":
        ret["type"] = "string"
    elif datatype == "COMPLEX":
        ret["type"] = "string"
    elif datatype in ("VECTOR", "VECTOR_COMPLEX"):
        ret["type"] = "string"
    else:
        ret["type"] = "string"

    if unit:
        ret["unit"] = unit

    if ret:
        cmd["returns"] = ret


def _extract_combo_options(fields: dict) -> tuple[list[str], list[str]]:
    """Extract combo display names and command values."""
    display_names = []
    cmd_values = []
    for i in range(1, 100):
        combo = fields.get(f"combo_def_{i}", "")
        if not combo:
            break
        display_names.append(combo)
        cmd_val = fields.get(f"cmd_def_{i}", combo)
        cmd_values.append(cmd_val)
    return display_names, cmd_values


def _handle_combo_write(cmd: dict, set_cmd: str, fields: dict) -> None:
    """Handle COMBO type as write command with enum parameter."""
    _, cmd_values = _extract_combo_options(fields)
    formatted = _format_set_cmd(set_cmd)
    cmd["scpi"] = formatted
    cmd["type"] = "write"
    if cmd_values:
        cmd["params"] = {
            "value": {
                "type": "enum",
                "options": cmd_values,
            }
        }


def _handle_combo_property(cmd: dict, set_cmd: str, get_cmd: str, fields: dict) -> None:
    """Handle COMBO type as property with enum parameter."""
    _, cmd_values = _extract_combo_options(fields)
    if set_cmd and get_cmd:
        cmd["getter"] = get_cmd
        cmd["setter"] = _format_set_cmd(set_cmd)
        cmd["type"] = "property"
    elif get_cmd:
        cmd["scpi"] = get_cmd
        cmd["type"] = "query"
    elif set_cmd:
        cmd["scpi"] = _format_set_cmd(set_cmd)
        cmd["type"] = "write"
    else:
        cmd["type"] = "query"

    if cmd_values:
        cmd["params"] = {
            "value": {
                "type": "enum",
                "options": cmd_values,
            }
        }
    cmd["returns"] = {"type": "string"}


# ---------------------------------------------------------------------------
# YAML output (manual to control formatting)
# ---------------------------------------------------------------------------

def yaml_escape(s: str) -> str:
    """Escape a string for YAML output."""
    if not s:
        return '""'
    # Quote if contains special chars
    if any(c in s for c in ":{}<>[]&*?|>!%@`#,") or s.startswith('"'):
        return f'"{s}"'
    return f'"{s}"'


def write_yaml(data: dict[str, Any], path: str) -> None:
    """Write a Galois YAML profile manually formatted."""
    lines: list[str] = []

    meta = data["instrument"]
    lines.append(f"# {meta['name']} Instrument Profile")
    lines.append(f"# Converted from Labber driver (MIT licensed, (c) 2015 Lab Control Software Scandinavia AB)")
    lines.append(f"# Original driver: {data.get('_source', 'unknown')}")
    if data.get("_has_python"):
        lines.append(f"# NOTE: Original Labber driver includes custom Python logic not captured here")
    lines.append("")

    # instrument:
    lines.append("instrument:")
    lines.append(f'  manufacturer: {yaml_escape(meta["manufacturer"])}')
    lines.append(f'  model: {yaml_escape(meta["model"])}')
    lines.append(f'  class: {meta["class"]}')
    lines.append(f'  description: {yaml_escape(meta["description"])}')
    lines.append("")

    # identity:
    identity = data["identity"]
    lines.append("identity:")
    lines.append(f'  query: "*IDN?"')
    lines.append(f'  pattern: {yaml_escape(identity["pattern"])}')
    lines.append("")

    # interfaces:
    lines.append("interfaces:")
    for iface in data.get("interfaces", [{"type": "gpib"}]):
        lines.append(f"  - type: {iface['type']}")
        if "default_address" in iface:
            lines.append(f"    default_address: {iface['default_address']}")
        if "port" in iface:
            lines.append(f"    port: {iface['port']}")
    lines.append("")

    # settings:
    settings = data["settings"]
    lines.append("settings:")
    lines.append(f'  timeout_ms: {settings["timeout_ms"]}')
    lines.append(f'  terminator: {yaml_escape(settings["terminator"])}')
    lines.append(f"  opc_query: false")
    lines.append("")

    # commands:
    commands = data.get("commands", {})
    if commands:
        lines.append("commands:")

        # Standard IEEE-488.2 commands first
        ieee_cmds = _standard_ieee_commands()
        lines.append("  # IEEE-488.2 Common Commands")
        for key, cmd in ieee_cmds.items():
            _write_command(lines, key, cmd)

        lines.append("")
        lines.append("  # Instrument-Specific Commands (converted from Labber)")
        for key, cmd in commands.items():
            _write_command(lines, key, cmd)

    lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _write_command(lines: list[str], key: str, cmd: dict) -> None:
    """Write a single command entry."""
    lines.append(f"  {key}:")

    if "scpi" in cmd:
        lines.append(f'    scpi: {yaml_escape(cmd["scpi"])}')
    if "getter" in cmd:
        lines.append(f'    getter: {yaml_escape(cmd["getter"])}')
    if "setter" in cmd:
        lines.append(f'    setter: {yaml_escape(cmd["setter"])}')

    lines.append(f'    type: {cmd["type"]}')

    if "params" in cmd:
        lines.append("    params:")
        for pname, pdef in cmd["params"].items():
            lines.append(f"      {pname}:")
            lines.append(f'        type: {pdef["type"]}')
            if "unit" in pdef:
                lines.append(f"        unit: {pdef['unit']}")
            if "min" in pdef:
                lines.append(f"        min: {pdef['min']}")
            if "max" in pdef:
                lines.append(f"        max: {pdef['max']}")
            if "options" in pdef:
                opts = ", ".join(f'"{o}"' for o in pdef["options"])
                lines.append(f"        options: [{opts}]")

    if "returns" in cmd:
        ret = cmd["returns"]
        lines.append("    returns:")
        lines.append(f'      type: {ret["type"]}')
        if "unit" in ret:
            lines.append(f"      unit: {ret['unit']}")

    lines.append(f'    description: {yaml_escape(cmd.get("description", key))}')


def _standard_ieee_commands() -> dict[str, dict]:
    """Return standard IEEE-488.2 commands included in every profile."""
    return {
        "reset": {
            "scpi": "*RST",
            "type": "write",
            "description": "Reset instrument to default settings",
        },
        "clear_status": {
            "scpi": "*CLS",
            "type": "write",
            "description": "Clear all event registers and error queue",
        },
        "identify": {
            "scpi": "*IDN?",
            "type": "query",
            "returns": {"type": "string"},
            "description": "Query instrument identification",
        },
        "operation_complete": {
            "scpi": "*OPC",
            "type": "write",
            "description": "Set OPC bit when all pending operations complete",
        },
        "operation_complete_query": {
            "scpi": "*OPC?",
            "type": "query",
            "returns": {"type": "int"},
            "description": "Return 1 when all pending operations complete",
        },
        "self_test": {
            "scpi": "*TST?",
            "type": "query",
            "returns": {"type": "int"},
            "description": "Perform self-test (0=pass)",
        },
    }


# ---------------------------------------------------------------------------
# Interface detection
# ---------------------------------------------------------------------------

def detect_interfaces(sections: dict) -> list[dict[str, Any]]:
    """Detect instrument interfaces from VISA settings."""
    visa = sections.get("VISA settings", {})
    interfaces = []

    use_visa = visa.get("use_visa", "True").lower() == "true"
    if not use_visa:
        return [{"type": "gpib"}]

    # Default to GPIB
    interfaces.append({"type": "gpib"})

    # Check for TCPIP
    tcpip_port = visa.get("tcpip_port", "")
    if tcpip_port:
        iface: dict[str, Any] = {"type": "ethernet"}
        try:
            iface["port"] = int(tcpip_port)
        except ValueError:
            pass
        interfaces.append(iface)

    return interfaces


# ---------------------------------------------------------------------------
# Identity pattern generation
# ---------------------------------------------------------------------------

def build_identity_pattern(meta: dict) -> str:
    """Build a regex pattern for *IDN? matching."""
    if meta["model_ids"]:
        # Use the model IDs to build a pattern
        escaped = [re.escape(m) for m in meta["model_ids"]]
        if len(escaped) == 1:
            return f"{re.escape(meta['manufacturer'])}.*{escaped[0]}"
        return f"{re.escape(meta['manufacturer'])}.*({'|'.join(escaped)})"

    # Fallback: use manufacturer + model
    return f"{re.escape(meta['manufacturer'])}.*{re.escape(meta['model'])}"


# ---------------------------------------------------------------------------
# Main conversion
# ---------------------------------------------------------------------------

SKIP_SECTIONS = {
    "General settings",
    "Model and options",
    "VISA settings",
}


def convert_driver(ini_path: str, driver_dir: str) -> dict[str, Any] | None:
    """Convert a single Labber INI file to a Galois profile dict."""
    sections = parse_labber_ini(ini_path)

    if "General settings" not in sections:
        return None

    meta = extract_instrument_meta(sections)
    visa_settings = extract_visa_settings(sections)
    interfaces = detect_interfaces(sections)
    inst_class = infer_class(meta["name"])

    # Check for Python driver
    has_python = False
    for f in os.listdir(driver_dir):
        if f.endswith(".py"):
            has_python = True
            break

    # Convert quantities to commands
    commands: dict[str, dict] = {}
    for section_name, fields in sections.items():
        if section_name in SKIP_SECTIONS:
            continue
        if "datatype" not in fields:
            continue

        cmd = convert_quantity(section_name, fields)
        if cmd is None:
            continue

        key = slugify(section_name)
        if not key or key in ("reset", "clear_status", "identify",
                               "operation_complete", "operation_complete_query",
                               "self_test"):
            # Skip if it would clash with standard IEEE commands
            key = f"inst_{key}" if key else "unknown"

        # Deduplicate
        if key in commands:
            key = f"{key}_2"

        commands[key] = cmd

    return {
        "instrument": {
            "name": meta["name"],
            "manufacturer": meta["manufacturer"],
            "model": meta["model"],
            "class": inst_class,
            "description": meta["name"],
        },
        "identity": {
            "pattern": build_identity_pattern(meta),
        },
        "interfaces": interfaces,
        "settings": {
            "timeout_ms": visa_settings["timeout_ms"],
            "terminator": visa_settings["terminator"],
        },
        "commands": commands,
        "_source": os.path.basename(driver_dir),
        "_has_python": has_python,
    }


def output_filename(driver_name: str) -> str:
    """Convert a Labber driver directory name to a YAML filename."""
    s = driver_name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = s.strip("_")
    return f"{s}.yaml"


def main() -> None:
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <labber-drivers-dir> <output-profiles-dir>")
        sys.exit(1)

    labber_dir = sys.argv[1]
    output_dir = sys.argv[2]

    if not os.path.isdir(labber_dir):
        print(f"Error: {labber_dir} is not a directory")
        sys.exit(1)

    os.makedirs(output_dir, exist_ok=True)

    # Track what we already have to skip duplicates
    existing = set()
    for f in os.listdir(output_dir):
        if f.endswith(".yaml"):
            existing.add(f)

    converted = 0
    skipped_existing = 0
    skipped_no_ini = 0
    errors = 0
    has_python_count = 0

    for entry in sorted(os.listdir(labber_dir)):
        driver_path = os.path.join(labber_dir, entry)
        if not os.path.isdir(driver_path):
            continue

        # Skip non-instrument directories
        if entry in ("Examples", "Manual", ".git", "__pycache__"):
            continue

        # Find INI file
        ini_file = None
        for f in os.listdir(driver_path):
            if f.endswith(".ini"):
                ini_file = os.path.join(driver_path, f)
                break

        if not ini_file:
            skipped_no_ini += 1
            continue

        out_name = output_filename(entry)
        if out_name in existing:
            skipped_existing += 1
            continue

        try:
            profile = convert_driver(ini_file, driver_path)
            if profile is None:
                errors += 1
                print(f"  SKIP  {entry} (no General settings)")
                continue

            out_path = os.path.join(output_dir, out_name)
            write_yaml(profile, out_path)
            converted += 1

            flag = " [has Python]" if profile["_has_python"] else ""
            print(f"  OK    {entry} -> {out_name}{flag}")

            if profile["_has_python"]:
                has_python_count += 1

        except Exception as e:
            errors += 1
            print(f"  ERROR {entry}: {e}")

    print(f"\n{'=' * 60}")
    print(f"Converted:        {converted}")
    print(f"Skipped existing: {skipped_existing}")
    print(f"Skipped no INI:   {skipped_no_ini}")
    print(f"Errors:           {errors}")
    print(f"With Python:      {has_python_count} (may need manual review)")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
