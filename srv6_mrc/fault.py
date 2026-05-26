"""Fault injection state tracking and topology-aware peer lookup.

Supports manual fault injection via `srctl fault` commands:
- Interface shutdown (bidirectional by default)
- tc/netem injection (loss, delay, etc.)
- State persistence to JSON for cleanup

Peer lookup reads `topology.clab.yaml` to find bidirectional link
endpoints (e.g., p0-spine01:Ethernet0 ↔ p0-leaf00:Ethernet4).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

# Default fault state file location (on docker host or local dev machine)
DEFAULT_FAULT_STATE_FILE = Path("/dev/shm/srv6-mrc-faults.json")


def _get_topo_dir() -> Path:
    """Get topology directory from SRV6_TOPO env or default."""
    import os
    topo_path_str = os.environ.get("SRV6_TOPO")
    if topo_path_str:
        return Path(topo_path_str).parent
    # Default to 4p-4x8
    here = Path(__file__).resolve()
    return here.parent.parent / "topologies" / "4p-4x8"

# SONiC interface naming: Ethernet0, 4, 8, 12, ... (increments of 4)
# Containerlab uses: eth1, eth2, eth3, ... (starts at 1)
def clab_to_sonic(clab_iface: str) -> str:
    """Convert containerlab ethN to SONiC EthernetM.
    
    Example: eth1 → Ethernet0, eth9 → Ethernet32
    """
    if not clab_iface.startswith("eth"):
        raise ValueError(f"Expected clab interface 'ethN', got {clab_iface!r}")
    try:
        n = int(clab_iface[3:])
    except ValueError:
        raise ValueError(f"Invalid clab interface number: {clab_iface!r}")
    return f"Ethernet{(n - 1) * 4}"


def sonic_to_clab(sonic_iface: str) -> str:
    """Convert SONiC EthernetM to containerlab ethN.
    
    Example: Ethernet0 → eth1, Ethernet32 → eth9
    """
    if not sonic_iface.startswith("Ethernet"):
        raise ValueError(f"Expected SONiC interface 'EthernetN', got {sonic_iface!r}")
    try:
        m = int(sonic_iface[8:])
    except ValueError:
        raise ValueError(f"Invalid SONiC interface number: {sonic_iface!r}")
    if m % 4 != 0:
        raise ValueError(f"SONiC interface must be multiple of 4, got {m}")
    return f"eth{m // 4 + 1}"


@dataclass
class InterfaceEndpoint:
    """One end of a link: (node, interface)."""
    node: str
    interface: str  # SONiC format (EthernetN)
    
    def __str__(self) -> str:
        return f"{self.node}:{self.interface}"
    
    def to_dict(self) -> dict[str, str]:
        return {"node": self.node, "interface": self.interface}
    
    @classmethod
    def from_dict(cls, d: dict[str, str]) -> InterfaceEndpoint:
        return cls(node=d["node"], interface=d["interface"])


@dataclass
class Fault:
    """A single injected fault with metadata."""
    id: str
    type: str  # "shutdown" or "netem"
    targets: list[InterfaceEndpoint]
    spec: Optional[str] = None  # netem spec string or "down" for shutdown
    bidirectional: bool = True
    applied_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    applied_by: str = "srctl fault"
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "targets": [t.to_dict() for t in self.targets],
            "spec": self.spec,
            "bidirectional": self.bidirectional,
            "applied_at": self.applied_at,
            "applied_by": self.applied_by,
        }
    
    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Fault:
        return cls(
            id=d["id"],
            type=d["type"],
            targets=[InterfaceEndpoint.from_dict(t) for t in d["targets"]],
            spec=d.get("spec"),
            bidirectional=d.get("bidirectional", True),
            applied_at=d["applied_at"],
            applied_by=d.get("applied_by", "srctl fault"),
        )


@dataclass
class FaultState:
    """Persistent fault state with JSON serialization."""
    version: int = 1
    faults: list[Fault] = field(default_factory=list)
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "faults": [f.to_dict() for f in self.faults],
        }
    
    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FaultState:
        return cls(
            version=d.get("version", 1),
            faults=[Fault.from_dict(f) for f in d.get("faults", [])],
        )
    
    @classmethod
    def load(cls, path: Path = DEFAULT_FAULT_STATE_FILE) -> FaultState:
        """Load from JSON file, or return empty state if file doesn't exist."""
        if not path.exists():
            return cls()
        with open(path, "r") as f:
            return cls.from_dict(json.load(f))
    
    def save(self, path: Path = DEFAULT_FAULT_STATE_FILE) -> None:
        """Save to JSON file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
    
    def add_fault(self, fault: Fault) -> None:
        """Add a fault to the state."""
        self.faults.append(fault)
    
    def remove_fault(self, fault_id: str) -> bool:
        """Remove a fault by ID. Returns True if found and removed."""
        for i, f in enumerate(self.faults):
            if f.id == fault_id:
                self.faults.pop(i)
                return True
        return False
    
    def clear_all(self) -> int:
        """Clear all faults. Returns count of faults removed."""
        count = len(self.faults)
        self.faults.clear()
        return count
    
    def find_by_node(self, node: str) -> list[Fault]:
        """Find all faults affecting a specific node."""
        return [f for f in self.faults if any(t.node == node for t in f.targets)]
    
    def find_by_interface(self, node: str, interface: str) -> list[Fault]:
        """Find all faults affecting a specific interface."""
        return [f for f in self.faults 
                if any(t.node == node and t.interface == interface for t in f.targets)]


class TopologyLinks:
    """Parse and query containerlab topology.clab.yaml for link endpoints."""
    
    def __init__(self, topo_yaml_path: Optional[Path] = None):
        if topo_yaml_path is None:
            topo_yaml_path = _get_topo_dir() / "topology.clab.yaml"
        self.topo_path = topo_yaml_path
        self._links: dict[tuple[str, str], tuple[str, str]] = {}
        self._load_links()
    
    def _load_links(self) -> None:
        """Load links from topology.clab.yaml."""
        if not self.topo_path.exists():
            raise FileNotFoundError(f"Topology file not found: {self.topo_path}")
        
        with open(self.topo_path, "r") as f:
            topo = yaml.safe_load(f)
        
        links = topo.get("topology", {}).get("links", [])
        for link in links:
            endpoints = link.get("endpoints", [])
            if len(endpoints) != 2:
                continue
            
            # Parse "node-a:ethN" format
            a_parts = endpoints[0].split(":")
            b_parts = endpoints[1].split(":")
            if len(a_parts) != 2 or len(b_parts) != 2:
                continue
            
            node_a, clab_iface_a = a_parts
            node_b, clab_iface_b = b_parts
            
            # Convert clab ethN to SONiC EthernetM
            sonic_iface_a = clab_to_sonic(clab_iface_a)
            sonic_iface_b = clab_to_sonic(clab_iface_b)
            
            # Store bidirectional lookup
            self._links[(node_a, sonic_iface_a)] = (node_b, sonic_iface_b)
            self._links[(node_b, sonic_iface_b)] = (node_a, sonic_iface_a)
    
    def get_peer(self, node: str, interface: str) -> Optional[tuple[str, str]]:
        """Get peer (node, interface) for a given endpoint.
        
        Returns None if no peer found (e.g., unconnected interface).
        Interface must be in SONiC format (EthernetN).
        """
        return self._links.get((node, interface))
    
    def get_all_interfaces(self, node: str) -> list[str]:
        """Get all SONiC interfaces for a node that have links."""
        return sorted({iface for (n, iface) in self._links.keys() if n == node})


def docker_exec(node: str, cmd: list[str]) -> subprocess.CompletedProcess:
    """Execute command in container via docker exec."""
    return subprocess.run(
        ["docker", "exec", node] + cmd,
        capture_output=True,
        text=True,
    )


def shutdown_interface(node: str, interface: str) -> None:
    """Shutdown a SONiC interface via ip link set down."""
    # Convert SONiC EthernetN to clab ethM for the in-container command
    clab_iface = sonic_to_clab(interface)
    result = docker_exec(node, ["ip", "link", "set", clab_iface, "down"])
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to shutdown {node}:{interface}: {result.stderr}"
        )


def startup_interface(node: str, interface: str) -> None:
    """Bring up a SONiC interface via ip link set up."""
    clab_iface = sonic_to_clab(interface)
    result = docker_exec(node, ["ip", "link", "set", clab_iface, "up"])
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to bring up {node}:{interface}: {result.stderr}"
        )


def generate_fault_id(state: FaultState) -> str:
    """Generate next sequential fault ID."""
    max_id = 0
    for f in state.faults:
        if f.id.startswith("fault-"):
            try:
                num = int(f.id.split("-")[1])
                max_id = max(max_id, num)
            except (ValueError, IndexError):
                pass
    return f"fault-{max_id + 1:03d}"
