#!/usr/bin/env python3
"""
Convert DBC (CAN database) files to Galois YAML profiles.

Usage:
    python scripts/dbc2galois.py /path/to/file.dbc /path/to/output/

Reads a DBC file using the cantools library, extracts CAN messages and
signals, and writes a corresponding YAML profile compatible with
galois-edge's profile_loader.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    import cantools
except ImportError:
    print(
        "Error: cantools library is required.\n"
        "Install it with:  pip install cantools\n"
        "  or:  pip install cantools[all]"
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Name conversion helpers
# ---------------------------------------------------------------------------

def to_snake_case(name: str) -> str:
    """Convert a DBC-style name (CamelCase or UPPER_CASE) to snake_case."""
    # Insert underscore before uppercase letters preceded by lowercase
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    # Insert underscore between consecutive uppercase followed by lowercase
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = s.strip("_")
    return s


def yaml_escape(s: str) -> str:
    """Escape a string for YAML output."""
    if not s:
        return '""'
    if any(c in s for c in ":{}<>[]&*?|>!%@`#,") or s.startswith('"'):
        return f'"{s}"'
    return f'"{s}"'


# ---------------------------------------------------------------------------
# DBC metadata extraction
# ---------------------------------------------------------------------------

def extract_metadata(db: Any, dbc_path: str) -> dict[str, str]:
    """Extract manufacturer and model metadata from DBC database."""
    filename = Path(dbc_path).stem

    # Try to get version/description from the database
    manufacturer = "Unknown"
    model = filename

    # cantools exposes db.dbc.attribute_definitions and db.dbc.attributes
    # for database-level attributes if available
    try:
        attrs = db.dbc.attributes if hasattr(db, "dbc") and db.dbc else {}
        if "Manufacturer" in attrs:
            manufacturer = str(attrs["Manufacturer"])
        elif "BusType" in attrs:
            manufacturer = str(attrs["BusType"])
    except (AttributeError, TypeError):
        pass

    return {
        "manufacturer": manufacturer,
        "model": model,
        "filename": Path(dbc_path).name,
    }


# ---------------------------------------------------------------------------
# Signal / message conversion
# ---------------------------------------------------------------------------

def convert_signal(signal: Any) -> dict[str, Any]:
    """Convert a cantools Signal to a Galois signal dict."""
    byte_order = "little_endian" if signal.byte_order == "little_endian" else "big_endian"

    sig: dict[str, Any] = {
        "start_bit": signal.start,
        "bit_length": signal.length,
        "byte_order": byte_order,
        "signed": bool(signal.is_signed),
        "scale": signal.scale,
        "offset": signal.offset,
    }
    if signal.unit:
        sig["unit"] = signal.unit
    else:
        sig["unit"] = ""

    return sig


def determine_direction(message: Any) -> str:
    """Determine message direction based on senders.

    If the sender is not 'Vector__XXX' (tool-generated default) and is
    identifiable, default to 'rx'.  In ambiguous cases, default to 'rx'.
    """
    return "rx"


def is_writable_signal(signal: Any, message: Any) -> bool:
    """Heuristic: consider a signal writable if the message has senders
    that aren't the default Vector placeholder, or if the signal has
    no explicit read-only markers.  Default: True (generate both
    get and set commands for every signal)."""
    return True


# ---------------------------------------------------------------------------
# YAML output (manual formatting, matching labber2galois.py style)
# ---------------------------------------------------------------------------

def write_yaml(data: dict[str, Any], path: str) -> None:
    """Write a Galois CAN YAML profile with manual formatting."""
    lines: list[str] = []

    meta = data["metadata"]
    lines.append(f"# CAN Profile: {meta['model']}")
    lines.append(f"# Auto-generated from {meta['filename']}")
    lines.append(f"# Source: {meta['filename']}")
    lines.append("")

    # protocol
    lines.append("protocol: can")
    lines.append("")

    # identity
    lines.append("identity:")
    lines.append(f"  manufacturer: {yaml_escape(meta['manufacturer'])}")
    lines.append(f"  model: {yaml_escape(meta['model'])}")
    lines.append(f"  description: {yaml_escape('Auto-generated from ' + meta['filename'])}")
    lines.append("")

    # connection
    conn = data["connection"]
    lines.append("connection:")
    lines.append(f"  channel: {yaml_escape(conn['channel'])}")
    lines.append(f"  interface: {yaml_escape(conn['interface'])}")
    lines.append(f"  bitrate: {conn['bitrate']}")
    lines.append("")

    # messages
    messages = data["messages"]
    if messages:
        lines.append("messages:")
        for msg_name, msg in messages.items():
            lines.append(f"  {msg_name}:")
            lines.append(f"    can_id: {msg['can_id']}")
            lines.append(f"    dlc: {msg['dlc']}")
            lines.append(f"    direction: {msg['direction']}")
            if msg.get("signals"):
                lines.append("    signals:")
                for sig_name, sig in msg["signals"].items():
                    lines.append(f"      {sig_name}:")
                    lines.append(f"        start_bit: {sig['start_bit']}")
                    lines.append(f"        bit_length: {sig['bit_length']}")
                    lines.append(f"        byte_order: {sig['byte_order']}")
                    lines.append(f"        signed: {'true' if sig['signed'] else 'false'}")
                    lines.append(f"        scale: {sig['scale']}")
                    lines.append(f"        offset: {sig['offset']}")
                    lines.append(f"        unit: {yaml_escape(sig['unit'])}")
        lines.append("")

    # commands
    commands = data["commands"]
    if commands:
        lines.append("commands:")
        for cmd_name, cmd in commands.items():
            lines.append(f"  {cmd_name}:")
            lines.append(f"    type: {cmd['type']}")
            if "reads" in cmd:
                reads_str = ", ".join(cmd["reads"])
                lines.append(f"    reads: [{reads_str}]")
            if "writes" in cmd:
                lines.append("    writes:")
                for w in cmd["writes"]:
                    lines.append(f"      - register: {w['register']}")
                    lines.append(f"        value: {yaml_escape(w['value'])}")
        lines.append("")

    # register_groups
    groups = data["register_groups"]
    if groups:
        lines.append("register_groups:")
        for grp_name, grp in groups.items():
            lines.append(f"  {grp_name}:")
            regs_str = ", ".join(grp["registers"])
            lines.append(f"    registers: [{regs_str}]")
            lines.append(f"    description: {yaml_escape(grp['description'])}")
        lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Main conversion
# ---------------------------------------------------------------------------

def convert_dbc(dbc_path: str) -> dict[str, Any]:
    """Convert a single DBC file to a Galois profile dict."""
    db = cantools.database.load_file(dbc_path)
    meta = extract_metadata(db, dbc_path)

    messages: dict[str, dict[str, Any]] = {}
    commands: dict[str, dict[str, Any]] = {}
    register_groups: dict[str, dict[str, Any]] = {}

    for message in db.messages:
        msg_key = to_snake_case(message.name)
        direction = determine_direction(message)

        # Build signals dict
        signals: dict[str, dict[str, Any]] = {}
        signal_names: list[str] = []

        for signal in message.signals:
            sig_key = to_snake_case(signal.name)
            signals[sig_key] = convert_signal(signal)
            signal_names.append(sig_key)

            # Generate get command (query) for every signal
            get_cmd_name = f"get_{sig_key}"
            commands[get_cmd_name] = {
                "type": "query",
                "reads": [sig_key],
            }

            # Generate set command (action) for writable signals
            if is_writable_signal(signal, message):
                set_cmd_name = f"set_{sig_key}"
                commands[set_cmd_name] = {
                    "type": "action",
                    "writes": [
                        {
                            "register": sig_key,
                            "value": "{" + sig_key + "}",
                        }
                    ],
                }

        messages[msg_key] = {
            "can_id": f"0x{message.frame_id:03X}",
            "dlc": message.length,
            "direction": direction,
            "signals": signals,
        }

        # Register group per message
        description = ""
        try:
            description = message.comment or message.name
        except AttributeError:
            description = message.name

        register_groups[msg_key] = {
            "registers": signal_names,
            "description": description,
        }

    return {
        "metadata": meta,
        "connection": {
            "channel": "can0",
            "interface": "socketcan",
            "bitrate": 500000,
        },
        "messages": messages,
        "commands": commands,
        "register_groups": register_groups,
    }


def output_filename(dbc_path: str) -> str:
    """Convert a DBC file path to a YAML output filename."""
    stem = Path(dbc_path).stem
    s = stem.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = s.strip("_")
    return f"{s}.yaml"


def main() -> None:
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <dbc-file> <output-dir>")
        sys.exit(1)

    dbc_path = sys.argv[1]
    output_dir = sys.argv[2]

    if not os.path.isfile(dbc_path):
        print(f"Error: {dbc_path} is not a file")
        sys.exit(1)

    if not dbc_path.lower().endswith(".dbc"):
        print(f"Warning: {dbc_path} does not have a .dbc extension")

    os.makedirs(output_dir, exist_ok=True)

    print(f"Converting DBC file: {dbc_path}")
    print(f"Output directory:    {output_dir}")
    print()

    try:
        profile = convert_dbc(dbc_path)
    except Exception as e:
        print(f"Error parsing DBC file: {e}")
        sys.exit(1)

    msg_count = len(profile["messages"])
    cmd_count = len(profile["commands"])
    grp_count = len(profile["register_groups"])

    print(f"  Messages:        {msg_count}")
    print(f"  Commands:        {cmd_count}")
    print(f"  Register groups: {grp_count}")
    print()

    out_name = output_filename(dbc_path)
    out_path = os.path.join(output_dir, out_name)

    write_yaml(profile, out_path)
    print(f"  OK  {Path(dbc_path).name} -> {out_name}")

    print()
    print(f"{'=' * 60}")
    print(f"Conversion complete: {out_path}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
