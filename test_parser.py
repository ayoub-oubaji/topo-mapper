#!/usr/bin/env python3
"""Self-test for topo_map.py — runs without real devices.

Feeds realistic `show cdp neighbors detail` samples through the parser,
checks link deduplication, and validates the DOT output.
Run:  python3 tests/test_parser.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import topo_map
from topo_map import (
    add_link,
    add_node,
    link_key,
    normalize_interface,
    parse_cdp_detail,
    write_topology_dot,
    write_topology_json,
)

SAMPLES = Path(__file__).resolve().parent.parent / "samples"

passed = failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        print(f"  FAIL: {name} {detail}")


print("== interface normalization ==")
check("GigabitEthernet0/1 -> Gi0/1",
      normalize_interface("GigabitEthernet0/1") == "Gi0/1")
check("Gi0/1 stays Gi0/1", normalize_interface("Gi0/1") == "Gi0/1")
check("FastEthernet0/5 -> Fa0/5",
      normalize_interface("FastEthernet0/5") == "Fa0/5")
check("Fa0/5 stays Fa0/5", normalize_interface("Fa0/5") == "Fa0/5")
check("TenGigabitEthernet1/0/1 -> Te1/0/1",
      normalize_interface("TenGigabitEthernet1/0/1") == "Te1/0/1")
check("eth0 -> Eth0", normalize_interface("eth0") == "Eth0")

print("== CDP parsing: switch_a ==")
nb_a = parse_cdp_detail((SAMPLES / "switch_a_cdp.txt").read_text())
check("3 neighbors parsed", len(nb_a) == 3, f"got {len(nb_a)}")
sw_b = next(n for n in nb_a if n["device_id"] == "SwitchB")
check("SwitchB ip", sw_b["ip"] == "192.168.1.2", sw_b)
check("SwitchB platform",
      sw_b["platform"] == "cisco WS-C2960-24TT-L", sw_b)
check("SwitchB local iface", sw_b["local_interface"] == "Gi0/1", sw_b)
check("SwitchB remote iface", sw_b["remote_interface"] == "Gi0/2", sw_b)
rtr = next(n for n in nb_a if n["device_id"] == "RouterA")
check("RouterA ip", rtr["ip"] == "192.168.1.1", rtr)
check("RouterA local Gi0/2", rtr["local_interface"] == "Gi0/2", rtr)
check("RouterA remote Gi0/0", rtr["remote_interface"] == "Gi0/0", rtr)
phone = next(n for n in nb_a if "SEP" in n["device_id"])
check("phone without IP -> None", phone["ip"] is None, phone)

print("== CDP parsing: switch_b ==")
nb_b = parse_cdp_detail((SAMPLES / "switch_b_cdp.txt").read_text())
check("2 neighbors parsed", len(nb_b) == 2, f"got {len(nb_b)}")
sw_a = next(n for n in nb_b if n["device_id"] == "SwitchA")
check("abbrev Gi0/2 normalized", sw_a["local_interface"] == "Gi0/2", sw_a)
check("abbrev Gi0/1 normalized", sw_a["remote_interface"] == "Gi0/1", sw_a)
cam = next(n for n in nb_b if n["device_id"] == "Camera-Floor2")
check("Fa0/5 normalized", cam["local_interface"] == "Fa0/5", cam)
check("eth0 -> Eth0", cam["remote_interface"] == "Eth0", cam)

print("== link deduplication ==")
nodes, links = {}, {}
add_node(nodes, "SwitchA", "192.168.1.3", "cisco WS-C2960-24TT-L")
for nb in nb_a:  # SwitchA's view
    add_node(nodes, nb["device_id"], nb["ip"], nb["platform"])
    add_link(links, "SwitchA", nb["local_interface"],
             nb["device_id"], nb["remote_interface"])
n_before = len(links)
for nb in nb_b:  # SwitchB's view (reverse link SwitchA<->SwitchB)
    add_node(nodes, nb["device_id"], nb["ip"], nb["platform"])
    add_link(links, "SwitchB", nb["local_interface"],
             nb["device_id"], nb["remote_interface"])
check("reverse link deduped (no new link for SwitchA<->SwitchB)",
      len(links) == n_before + 1,  # only Camera-Floor2 link is new
      f"before={n_before} after={len(links)}")
ab = [l for l in links.values()
      if {l["node_a"], l["node_b"]} == {"SwitchA", "SwitchB"}]
check("exactly one SwitchA<->SwitchB link", len(ab) == 1, ab)
check("deduped link interfaces",
      ab and ab[0]["interface_a"] == "Gi0/1" and ab[0]["interface_b"] == "Gi0/2",
      ab)
check("link_key symmetric",
      link_key("A", "Gi0/1", "B", "Gi0/2") == link_key("B", "Gi0/2", "A", "Gi0/1"))

print("== JSON + DOT output ==")
import json
import tempfile

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    write_topology_json(nodes, links, tmp / "topology.json")
    write_topology_dot(nodes, links, tmp / "topology.dot")
    data = json.loads((tmp / "topology.json").read_text())
    check("json has nodes+links",
          "nodes" in data and "links" in data, list(data.keys()))
    check("json node fields",
          all(set(n) == {"name", "ip", "platform"} for n in data["nodes"]))
    check("json link fields",
          all(set(l) == {"node_a", "interface_a", "node_b", "interface_b"}
              for l in data["links"]))
    dot = (tmp / "topology.dot").read_text()
    check("dot starts with graph decl",
          dot.startswith("graph topology {"), dot[:40])
    check("dot contains nodes",
          '"SwitchA"' in dot and '"SwitchB"' in dot and '"RouterA"' in dot)
    check("dot contains edge with iface labels",
          "Gi0/1 -- Gi0/2" in dot or "Gi0/1" in dot, dot[:200])
    check("dot balanced braces", dot.count("{") == dot.count("}"))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
