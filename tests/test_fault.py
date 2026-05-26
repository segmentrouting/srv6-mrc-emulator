"""Tests for srv6_mrc.fault module."""

import json
import tempfile
import unittest
from pathlib import Path

from srv6_mrc import fault


class TestInterfaceConversion(unittest.TestCase):
    """Test containerlab ↔ SONiC interface name conversion."""
    
    def test_clab_to_sonic(self):
        self.assertEqual(fault.clab_to_sonic("eth1"), "Ethernet0")
        self.assertEqual(fault.clab_to_sonic("eth2"), "Ethernet4")
        self.assertEqual(fault.clab_to_sonic("eth9"), "Ethernet32")
        self.assertEqual(fault.clab_to_sonic("eth10"), "Ethernet36")
    
    def test_sonic_to_clab(self):
        self.assertEqual(fault.sonic_to_clab("Ethernet0"), "eth1")
        self.assertEqual(fault.sonic_to_clab("Ethernet4"), "eth2")
        self.assertEqual(fault.sonic_to_clab("Ethernet32"), "eth9")
        self.assertEqual(fault.sonic_to_clab("Ethernet36"), "eth10")
    
    def test_round_trip(self):
        for eth_n in range(1, 11):
            clab_iface = f"eth{eth_n}"
            sonic_iface = fault.clab_to_sonic(clab_iface)
            back_to_clab = fault.sonic_to_clab(sonic_iface)
            self.assertEqual(back_to_clab, clab_iface)
    
    def test_invalid_clab_format(self):
        with self.assertRaises(ValueError):
            fault.clab_to_sonic("Ethernet0")
        with self.assertRaises(ValueError):
            fault.clab_to_sonic("eth")
        with self.assertRaises(ValueError):
            fault.clab_to_sonic("ethX")
    
    def test_invalid_sonic_format(self):
        with self.assertRaises(ValueError):
            fault.sonic_to_clab("eth1")
        with self.assertRaises(ValueError):
            fault.sonic_to_clab("Ethernet")
        with self.assertRaises(ValueError):
            fault.sonic_to_clab("Ethernet5")  # not multiple of 4


class TestFaultState(unittest.TestCase):
    """Test fault state persistence and queries."""
    
    def test_empty_state(self):
        state = fault.FaultState()
        self.assertEqual(len(state.faults), 0)
        self.assertEqual(state.version, 1)
    
    def test_add_fault(self):
        state = fault.FaultState()
        f = fault.Fault(
            id="fault-001",
            type="shutdown",
            targets=[fault.InterfaceEndpoint("p0-spine01", "Ethernet0")],
            spec="down",
        )
        state.add_fault(f)
        self.assertEqual(len(state.faults), 1)
        self.assertEqual(state.faults[0].id, "fault-001")
    
    def test_remove_fault(self):
        state = fault.FaultState()
        f = fault.Fault(
            id="fault-001",
            type="shutdown",
            targets=[fault.InterfaceEndpoint("p0-spine01", "Ethernet0")],
        )
        state.add_fault(f)
        removed = state.remove_fault("fault-001")
        self.assertTrue(removed)
        self.assertEqual(len(state.faults), 0)
        
        # Try removing again
        removed = state.remove_fault("fault-001")
        self.assertFalse(removed)
    
    def test_find_by_node(self):
        state = fault.FaultState()
        f1 = fault.Fault(
            id="fault-001",
            type="shutdown",
            targets=[
                fault.InterfaceEndpoint("p0-spine01", "Ethernet0"),
                fault.InterfaceEndpoint("p0-leaf00", "Ethernet4"),
            ],
        )
        f2 = fault.Fault(
            id="fault-002",
            type="shutdown",
            targets=[fault.InterfaceEndpoint("p1-spine02", "Ethernet8")],
        )
        state.add_fault(f1)
        state.add_fault(f2)
        
        results = state.find_by_node("p0-spine01")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].id, "fault-001")
        
        results = state.find_by_node("p0-leaf00")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].id, "fault-001")
        
        results = state.find_by_node("p2-leaf03")
        self.assertEqual(len(results), 0)
    
    def test_find_by_interface(self):
        state = fault.FaultState()
        f = fault.Fault(
            id="fault-001",
            type="shutdown",
            targets=[
                fault.InterfaceEndpoint("p0-spine01", "Ethernet0"),
                fault.InterfaceEndpoint("p0-spine01", "Ethernet4"),
            ],
        )
        state.add_fault(f)
        
        results = state.find_by_interface("p0-spine01", "Ethernet0")
        self.assertEqual(len(results), 1)
        
        results = state.find_by_interface("p0-spine01", "Ethernet8")
        self.assertEqual(len(results), 0)
    
    def test_serialization(self):
        state = fault.FaultState()
        f = fault.Fault(
            id="fault-001",
            type="shutdown",
            targets=[fault.InterfaceEndpoint("p0-spine01", "Ethernet0")],
            spec="down",
            bidirectional=True,
        )
        state.add_fault(f)
        
        # Serialize
        d = state.to_dict()
        self.assertEqual(d["version"], 1)
        self.assertEqual(len(d["faults"]), 1)
        self.assertEqual(d["faults"][0]["id"], "fault-001")
        
        # Deserialize
        state2 = fault.FaultState.from_dict(d)
        self.assertEqual(state2.version, 1)
        self.assertEqual(len(state2.faults), 1)
        self.assertEqual(state2.faults[0].id, "fault-001")
        self.assertEqual(state2.faults[0].targets[0].node, "p0-spine01")
    
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "test-faults.json"
            
            # Create and save
            state = fault.FaultState()
            f = fault.Fault(
                id="fault-001",
                type="shutdown",
                targets=[fault.InterfaceEndpoint("p0-spine01", "Ethernet0")],
            )
            state.add_fault(f)
            state.save(state_file)
            
            # Load
            state2 = fault.FaultState.load(state_file)
            self.assertEqual(len(state2.faults), 1)
            self.assertEqual(state2.faults[0].id, "fault-001")
    
    def test_load_nonexistent_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "nonexistent.json"
            state = fault.FaultState.load(state_file)
            self.assertEqual(len(state.faults), 0)  # Returns empty state


class TestGenerateFaultId(unittest.TestCase):
    """Test fault ID generation."""
    
    def test_first_fault(self):
        state = fault.FaultState()
        fault_id = fault.generate_fault_id(state)
        self.assertEqual(fault_id, "fault-001")
    
    def test_sequential_ids(self):
        state = fault.FaultState()
        for i in range(1, 10):
            fault_id = fault.generate_fault_id(state)
            self.assertEqual(fault_id, f"fault-{i:03d}")
            # Add a dummy fault so next ID increments
            f = fault.Fault(
                id=fault_id,
                type="shutdown",
                targets=[fault.InterfaceEndpoint("node", "Ethernet0")],
            )
            state.add_fault(f)
    
    def test_handles_gaps(self):
        state = fault.FaultState()
        # Add non-sequential faults
        state.add_fault(fault.Fault(id="fault-001", type="shutdown", targets=[]))
        state.add_fault(fault.Fault(id="fault-005", type="shutdown", targets=[]))
        
        # Next ID should be fault-006 (max + 1)
        fault_id = fault.generate_fault_id(state)
        self.assertEqual(fault_id, "fault-006")


class TestTopologyLinks(unittest.TestCase):
    """Test topology link parsing (requires actual topology.clab.yaml)."""
    
    def test_load_links(self):
        # This test requires the actual topology file
        try:
            topo = fault.TopologyLinks()
            # Basic sanity check: should have loaded some links
            interfaces = topo.get_all_interfaces("p0-spine00")
            self.assertGreater(len(interfaces), 0)
        except FileNotFoundError:
            self.skipTest("topology.clab.yaml not found")
    
    def test_get_peer(self):
        try:
            topo = fault.TopologyLinks()
            # Check a known link from 4p-4x8
            peer = topo.get_peer("p0-spine00", "Ethernet0")
            if peer:
                self.assertEqual(peer[0], "p0-leaf00")
                self.assertEqual(peer[1], "Ethernet0")
        except FileNotFoundError:
            self.skipTest("topology.clab.yaml not found")


if __name__ == "__main__":
    unittest.main()
