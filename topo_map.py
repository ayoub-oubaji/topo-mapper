#!/usr/bin/env python3
"""
topo-mapper — Network Topology Mapper
=====================================
Discovers the L2/L3 topology of a Cisco network using CDP.

How it works:
  1. Connects to each seed device (from --inventory) over SSH with Netmiko.
  2. Runs `show cdp neighbors detail` and parses every neighbor entry:
     device ID, management IP, platform, local interface, remote interface.
  3. Optionally crawls one level deep: discovered neighbors that have an IP
     are queried the same way (skipped gracefully if unreachable).
  4. Bidirectional links (A:Gi0/1 <-> B:Gi0/2 seen from both sides) are
     deduplicated and stored once.
  5. Writes topology.json (nodes + links) and topology.dot (Graphviz DOT).

Usage:
    cp devices.example.json devices.json   # then edit with real devices
    python3 topo_map.py
    python3 topo_map.py --inventory lab.json --out-dir ./out --username admin
    python3 topo_map.py --no-crawl          # only query the seed devices

Render the DOT file with Graphviz:
    dot -Tpng topology.dot -o topology.png
"""

import argparse
import getpass
import json
import re
import sys
from pathlib import Path

try:
    from netmiko import ConnectHandler
    from netmiko.exceptions import (
        NetmikoAuthenticationException,
        NetmikoTimeoutException,
    )
    _NETMIKO_AVAILABLE = True
except ImportError:
    # Netmiko is only needed for live SSH discovery. The parsing, topology
    # model, and output writers below work without it (and are unit-tested).
    _NETMIKO_AVAILABLE = False


# ---------------------------------------------------------------------------
# CDP parsing
# ---------------------------------------------------------------------------

# Canonical short names for interface types (handles full names and
# common abbreviations: GigabitEthernet/Gi, FastEthernet/Fa, Eth, Te, ...).
_IFACE_ABBR = {
    "gigabitethernet": "Gi",
    "gig": "Gi",
    "gi": "Gi",
    "fastethernet": "Fa",
    "fa": "Fa",
    "ethernet": "Eth",
    "eth": "Eth",
    "tengigabitethernet": "Te",
    "tengig": "Te",
    "te": "Te",
    "tengigabit": "Te",
    "serial": "Se",
    "se": "Se",
    "port-channel": "Po",
    "po": "Po",
    "loopback": "Lo",
    "lo": "Lo",
    "vlan": "Vl",
    "vl": "Vl",
    "tunnel": "Tu",
    "tu": "Tu",
}

_RE_DEVICE_ID = re.compile(r"^Device ID:\s*(.+?)\s*$", re.MULTILINE)
_RE_IP = re.compile(r"IP address:\s*([0-9]{1,3}(?:\.[0-9]{1,3}){3})")
_RE_PLATFORM = re.compile(r"^Platform:\s*([^,]+)", re.MULTILINE)
_RE_LOCAL_IFACE = re.compile(r"^Interface:\s*([^,]+),", re.MULTILINE)
_RE_REMOTE_IFACE = re.compile(r"Port ID \(outgoing port\):\s*(.+?)\s*$", re.MULTILINE)
_RE_ENTRY_SPLIT = re.compile(r"^-{5,}\s*$", re.MULTILINE)


def normalize_interface(name: str) -> str:
    """Normalize an interface name to a canonical short form.

    Examples: 'GigabitEthernet0/1' -> 'Gi0/1', 'Gi0/1' -> 'Gi0/1',
              'FastEthernet0/5' -> 'Fa0/5', 'Eth1/2' -> 'Eth1/2'.
    Unknown formats are returned stripped but unchanged.
    """
    name = name.strip()
    m = re.match(r"^([A-Za-z\- ]+?)\s*([\d/\.]+)$", name)
    if not m:
        return name
    kind, number = m.group(1).strip().lower(), m.group(2)
    short = _IFACE_ABBR.get(kind)
    if not short:
        return name
    return f"{short}{number}"


def parse_cdp_detail(output: str) -> list:
    """Parse `show cdp neighbors detail` output.

    Returns a list of dicts:
        {"device_id": str, "ip": str|None, "platform": str|None,
         "local_interface": str, "remote_interface": str}
    Entries missing a device ID or interfaces are skipped.
    """
    neighbors = []
    for entry in _RE_ENTRY_SPLIT.split(output):
        if "Device ID:" not in entry:
            continue
        m_id = _RE_DEVICE_ID.search(entry)
        m_local = _RE_LOCAL_IFACE.search(entry)
        m_remote = _RE_REMOTE_IFACE.search(entry)
        if not (m_id and m_local and m_remote):
            continue
        m_ip = _RE_IP.search(entry)
        m_plat = _RE_PLATFORM.search(entry)
        neighbors.append(
            {
                "device_id": m_id.group(1).strip(),
                # Strip any domain suffix for a cleaner node name,
                # but keep the raw value too for reference.
                "ip": m_ip.group(1) if m_ip else None,
                "platform": m_plat.group(1).strip() if m_plat else None,
                "local_interface": normalize_interface(m_local.group(1)),
                "remote_interface": normalize_interface(m_remote.group(1)),
            }
        )
    return neighbors


# ---------------------------------------------------------------------------
# Topology model
# ---------------------------------------------------------------------------

def link_key(node_a: str, iface_a: str, node_b: str, iface_b: str) -> tuple:
    """Canonical key for a link — identical for both directions."""
    return tuple(sorted([(node_a, iface_a), (node_b, iface_b)]))


def add_node(nodes: dict, name: str, ip=None, platform=None):
    """Add a node, filling in missing details when learned later."""
    node = nodes.setdefault(name, {"name": name, "ip": None, "platform": None})
    if ip and not node["ip"]:
        node["ip"] = ip
    if platform and not node["platform"]:
        node["platform"] = platform


def add_link(links: dict, node_a: str, iface_a: str, node_b: str, iface_b: str):
    """Add a link, deduplicating bidirectional discoveries."""
    key = link_key(node_a, iface_a, node_b, iface_b)
    if key not in links:
        (na, ia), (nb, ib) = key
        links[key] = {
            "node_a": na,
            "interface_a": ia,
            "node_b": nb,
            "interface_b": ib,
        }


def query_device(device: dict, username: str, password: str, timeout: int = 20):
    """SSH to one device and return (neighbors, error_message)."""
    if not _NETMIKO_AVAILABLE:
        return None, "netmiko is not installed (pip install -r requirements.txt)"
    try:
        conn = ConnectHandler(
            device_type=device.get("device_type", "cisco_ios"),
            host=device["host"],
            username=username,
            password=password,
            timeout=timeout,
        )
    except NetmikoAuthenticationException:
        return None, "authentication failed"
    except NetmikoTimeoutException:
        return None, "connection timed out"
    except Exception as exc:  # noqa: BLE001 - report plainly, keep crawling
        return None, f"connection error: {exc}"
    try:
        output = conn.send_command("show cdp neighbors detail")
    except Exception as exc:  # noqa: BLE001
        return None, f"command failed: {exc}"
    finally:
        conn.disconnect()
    return parse_cdp_detail(output), None


def discover(devices: list, username: str, password: str, crawl: bool = True,
             timeout: int = 20):
    """Discover topology starting from the seed devices.

    Crawls one level deep: neighbors of seed devices are queried too
    (only if they advertise an IP). Every device is queried at most once;
    failures are recorded and skipped gracefully.
    """
    nodes, links = {}, {}
    seen = set()  # hosts already queried
    queue = [(dev, 0) for dev in devices]
    stats = {"queried": 0, "ok": 0, "failed": 0, "failures": []}

    while queue:
        device, depth = queue.pop(0)
        host = device["host"]
        if host in seen:
            continue
        seen.add(host)
        name = device.get("name") or host
        stats["queried"] += 1

        neighbors, err = query_device(device, username, password, timeout)
        if err:
            stats["failed"] += 1
            stats["failures"].append({"device": name, "host": host, "error": err})
            print(f"[!] {name} ({host}): {err}", file=sys.stderr)
            continue

        stats["ok"] += 1
        print(f"[+] {name} ({host}): {len(neighbors)} CDP neighbor(s)")
        add_node(nodes, name)
        for nb in neighbors:
            add_node(nodes, nb["device_id"], nb["ip"], nb["platform"])
            add_link(links, name, nb["local_interface"],
                     nb["device_id"], nb["remote_interface"])
            if crawl and depth == 0 and nb["ip"] and nb["ip"] not in seen:
                queue.append(
                    (
                        {
                            "host": nb["ip"],
                            "name": nb["device_id"],
                            "device_type": device.get("device_type", "cisco_ios"),
                        },
                        1,
                    )
                )

    return nodes, links, stats


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def _dot_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def write_topology_json(nodes: dict, links: dict, path: Path):
    data = {
        "nodes": sorted(nodes.values(), key=lambda n: n["name"]),
        "links": sorted(
            links.values(), key=lambda l: (l["node_a"], l["node_b"])
        ),
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def write_topology_dot(nodes: dict, links: dict, path: Path):
    lines = [
        "graph topology {",
        "    // Generated by topo-mapper",
        '    rankdir=LR;',
        '    node [shape=box, style="rounded,filled", fillcolor="#e8f0fe"];',
        "",
    ]
    for node in sorted(nodes.values(), key=lambda n: n["name"]):
        label_parts = [node["name"]]
        if node.get("platform"):
            label_parts.append(node["platform"])
        if node.get("ip"):
            label_parts.append(node["ip"])
        label = "\\n".join(_dot_escape(p) for p in label_parts)
        lines.append(f'    "{_dot_escape(node["name"])}" [label="{label}"];')
    lines.append("")
    for link in sorted(links.values(), key=lambda l: (l["node_a"], l["node_b"])):
        a = _dot_escape(link["node_a"])
        b = _dot_escape(link["node_b"])
        edge_label = (
            f"{_dot_escape(link['interface_a'])} -- "
            f"{_dot_escape(link['interface_b'])}"
        )
        lines.append(f'    "{a}" -- "{b}" [label="{edge_label}"];')
    lines.append("}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Discover network topology via CDP and render it as JSON + Graphviz DOT."
    )
    parser.add_argument("--inventory", default="devices.json",
                        help="JSON inventory file (default: devices.json)")
    parser.add_argument("--out-dir", default="topology-out",
                        help="Output directory for topology.json/.dot (default: topology-out/)")
    parser.add_argument("--username", default="",
                        help="SSH username (prompted if omitted)")
    parser.add_argument("--no-crawl", action="store_true",
                        help="Only query the seed devices, do not crawl discovered neighbors")
    parser.add_argument("--timeout", type=int, default=20,
                        help="SSH/command timeout in seconds (default: 20)")
    args = parser.parse_args()

    inventory = Path(args.inventory)
    if not inventory.exists():
        print(f"Error: {inventory} not found.", file=sys.stderr)
        print("Copy devices.example.json to devices.json and fill in your devices.",
              file=sys.stderr)
        return 1

    try:
        devices = json.loads(inventory.read_text(encoding="utf-8"))["devices"]
    except (json.JSONDecodeError, KeyError) as exc:
        print(f"Error: invalid inventory file: {exc}", file=sys.stderr)
        return 1
    if not devices:
        print("Error: no devices in inventory.", file=sys.stderr)
        return 1

    username = args.username or input("SSH username: ")
    password = getpass.getpass("SSH password: ")

    print(f"Discovering topology from {len(devices)} seed device(s) "
          f"(crawl={'off' if args.no_crawl else 'on'})...\n")
    nodes, links, stats = discover(
        devices, username, password,
        crawl=not args.no_crawl, timeout=args.timeout,
    )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "topology.json"
    dot_path = out_dir / "topology.dot"
    write_topology_json(nodes, links, json_path)
    write_topology_dot(nodes, links, dot_path)

    print(f"\n--- Summary ---")
    print(f"Devices queried : {stats['queried']}")
    print(f"  succeeded     : {stats['ok']}")
    print(f"  failed        : {stats['failed']}")
    for f in stats["failures"]:
        print(f"    - {f['device']} ({f['host']}): {f['error']}")
    print(f"Nodes discovered: {len(nodes)}")
    print(f"Links discovered: {len(links)}")
    print(f"Wrote {json_path} and {dot_path}")
    print("Render with: dot -Tpng topology.dot -o topology.png")
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
