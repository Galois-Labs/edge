"""EdgeContext — handle passed to every MCP tool implementation.

Phase 1 carries direct references to CapabilityManager, CommandHandler,
InstrumentManager. Phase 2 will add `caller_jwt` and an authorize() hook.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ..capability_manager import CapabilityManager
    from ..command_handler import CommandHandler


@dataclass
class EdgeContext:
    """Per-call references to the daemon's in-process subsystems.

    Tool implementations call into these directly rather than dialling
    localhost gRPC; the servicer is in the same process and the proto
    translation cost is wasted CPU when in-memory dispatch is available.
    """

    capability_manager: "CapabilityManager"
    command_handler: "CommandHandler"
    instrument_manager: Any
    edge_id: str = ""
    edge_name: str = ""
    version: str = "1.0.0"
    sweep_state: Optional[Any] = None
