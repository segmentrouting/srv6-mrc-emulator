import unittest

from srv6_mrc.reorder import FlowStats, ReorderTracker
from srv6_mrc.topo import FlowKey


F = FlowKey("src", "dst", 9000, 9999)


class TestFlowStats(unittest.TestCase):
    def test_perfectly_in_order(self):
        s = FlowStats(flow=F)
        for i in range(100):
            s.observe(i, plane=i % 4)
        self.assertEqual(s.received, 100)
        self.assertEqual(s.duplicates, 0)
        self.assertEqual(s.loss, 0)
        self.assertEqual(s.reorder_max, 0)
        self.assertEqual(s.reorder_mean, 0.0)
        self.assertEqual(s.reorder_hist, {0: 100})
        self.assertEqual(sum(s.per_plane.values()), 100)
        for p in range(4):
            self.assertEqual(s.per_plane[p], 25)

    def test_swapped_pair(self):
        # 0, 1, 3, 2, 4, 5 -> seq 2 arrives 1 behind seq 3.
        s = FlowStats(flow=F)
        for seq in [0, 1, 3, 2, 4, 5]:
            s.observe(seq)
        self.assertEqual(s.received, 6)
        self.assertEqual(s.loss, 0)
        self.assertEqual(s.reorder_hist, {0: 5, 1: 1})
        self.assertEqual(s.reorder_max, 1)

    def test_late_packet_large_delta(self):
        s = FlowStats(flow=F)
        for seq in [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]:
            s.observe(seq)
        # seq 3 arrives way after 9 -> already counted; now a stray:
        s.observe(3)
        # 3 is in window -> duplicate, NOT a reorder event.
        self.assertEqual(s.duplicates, 1)
        self.assertEqual(s.reorder_max, 0)

        # Now a true late: seq 2 (in window already too — also dup).
        s.observe(2)
        self.assertEqual(s.duplicates, 2)

    def test_loss(self):
        s = FlowStats(flow=F)
        for seq in [0, 1, 2, 4, 5]:   # 3 missing
            s.observe(seq)
        self.assertEqual(s.expected, 6)   # 0..5 inclusive
        self.assertEqual(s.received, 5)
        self.assertEqual(s.loss, 1)

    def test_reverse_order(self):
        s = FlowStats(flow=F)
        for seq in [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]:
            s.observe(seq)
        # seq 9 is in-order (bin 0), then 8 is delta=1, 7 is delta=2, ...
        self.assertEqual(s.reorder_hist[0], 1)
        self.assertEqual(s.reorder_hist[1], 1)
        self.assertEqual(s.reorder_hist[9], 1)
        self.assertEqual(s.reorder_max, 9)

    def test_percentile(self):
        s = FlowStats(flow=F)
        # 90 in-order, 10 with delta=5.
        for i in range(90):
            s.observe(i)
        # Now inject 10 late packets behind a high seq.
        s.observe(200)
        for d in range(10):
            s.observe(200 - 5)   # delta = 5; all duplicates after the first
        # Only the first reaches the histogram; rest are dups.
        # So actually: 91 at bin 0, 1 at bin 5.
        self.assertEqual(s.reorder_hist.get(0), 91)
        self.assertEqual(s.reorder_hist.get(5), 1)
        # 99th percentile of {0:91, 5:1} -> 5 (the single tail packet pushes
        # 92*0.99 = 91.08 over the bin-0 cumulative of 91).
        self.assertEqual(s.reorder_percentile(99), 5)
        # 50th percentile: well within bin 0.
        self.assertEqual(s.reorder_percentile(50), 0)
        with self.assertRaises(ValueError):
            s.reorder_percentile(0)
        with self.assertRaises(ValueError):
            s.reorder_percentile(150)

    def test_duplicate_does_not_count_as_recv(self):
        s = FlowStats(flow=F)
        s.observe(0)
        s.observe(0)
        s.observe(0)
        self.assertEqual(s.received, 1)
        self.assertEqual(s.duplicates, 2)

    def test_empty(self):
        s = FlowStats(flow=F)
        self.assertEqual(s.received, 0)
        self.assertEqual(s.expected, 0)
        self.assertEqual(s.loss, 0)
        self.assertEqual(s.reorder_max, 0)
        self.assertEqual(s.reorder_mean, 0.0)
        self.assertEqual(s.reorder_percentile(99), 0)

    def test_to_dict_shape(self):
        s = FlowStats(flow=F)
        s.observe(0, plane=0)
        s.observe(1, plane=1)
        s.observe(2, plane=2)
        d = s.to_dict()
        self.assertEqual(d["src"], "src")
        self.assertEqual(d["dst"], "dst")
        self.assertEqual(d["sport"], 9000)
        self.assertEqual(d["dport"], 9999)
        self.assertEqual(d["received"], 3)
        self.assertEqual(d["loss"], 0)
        self.assertEqual(d["per_plane_recv"], {0: 1, 1: 1, 2: 1})
        self.assertEqual(d["reorder_hist"], {0: 3})


class TestTracker(unittest.TestCase):
    def test_demultiplex(self):
        t = ReorderTracker()
        a = FlowKey("a", "b", 1, 2)
        b = FlowKey("a", "b", 3, 2)
        t.observe(a, 0, plane=0)
        t.observe(a, 1, plane=1)
        t.observe(b, 0, plane=2)
        flows = {(f.flow, f.received) for f in t.flows()}
        self.assertEqual(flows, {(a, 2), (b, 1)})

    def test_to_dict_lists_flows(self):
        t = ReorderTracker()
        t.observe(F, 0)
        t.observe(F, 1)
        d = t.to_dict()
        self.assertEqual(len(d["flows"]), 1)
        self.assertEqual(d["flows"][0]["received"], 2)


if __name__ == "__main__":
    unittest.main()
