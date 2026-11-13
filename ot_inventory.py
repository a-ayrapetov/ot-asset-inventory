#!/usr/bin/env python3
"""
ot-asset-inventory: passive OT asset inventory from switch show-command output.

Save read-only show commands from each switch into one folder per switch and get
a per-port device list: switch, port, description, VLAN, MAC, IP, vendor, flags.
It correlates four tables per switch:

  ARP table              IP to MAC
  MAC address table      MAC to port and VLAN
  show interfaces status port to VLAN, status and description
  CDP / LLDP detail      port to neighbour identity

Trunks and ports with an infrastructure neighbour (router, switch, bridge, access
point) are kept out of the endpoint list and reported as infrastructure links.

Nothing is sent to the OT endpoints. Text parsing only, standard library only,
never connects to anything.

Usage
  ot-inventory collect/ --oui oui.csv --csv inventory.csv --json inventory.json
  ot-inventory diff old.csv new.csv --csv changes.csv

Exit codes: collect 0 built, 2 error. diff 0 no changes, 1 changes, 2 error.
"""
import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

NO_OUI = "(no OUI file)"
FIELDS = ["switch", "port", "port_description", "vlan", "mac", "ip", "vendor", "flags"]
INFRA_FIELDS = ["switch", "port", "port_description", "vlan", "reason", "neighbor",
                "neighbor_port", "neighbor_platform", "neighbor_ip", "macs"]
DIFF_FIELDS = ["change", "mac", "ip", "vendor", "old_switch", "old_port", "old_vlan",
               "new_switch", "new_port", "new_vlan"]

# File name start (lower case, '-' and ' ' read as '_') -> table kind.
FILE_KINDS = [
    ("show_ip_arp", "arp"), ("show_arp", "arp"),
    ("show_mac_address_table", "mac"),
    ("show_interfaces_status", "status"), ("show_interface_status", "status"),
    ("show_cdp_neighbors_detail", "cdp"),
    ("show_lldp_neighbors_detail", "lldp"),
]
KINDS = ["arp", "mac", "status", "cdp", "lldp"]

# Neighbour capabilities that make a port an infrastructure link.
CDP_INFRA = {"router", "switch", "trans-bridge", "source-route-bridge", "repeater"}
LLDP_INFRA = {"R", "B", "W", "P", "C"}  # router, bridge, WLAN AP, repeater, DOCSIS

_INTF = {
    "GigabitEthernet": ("gi", "gig", "gige"),
    "FastEthernet": ("fa",),
    "TenGigabitEthernet": ("te", "ten", "tengig", "tengige"),
    "TwoGigabitEthernet": ("tw", "twogige"),
    "FiveGigabitEthernet": ("fi", "fivegige"),
    "TwentyFiveGigE": ("twe", "twentyfivegigabitethernet"),
    "FortyGigabitEthernet": ("fo", "fortygige"),
    "HundredGigE": ("hu", "hundredgigabitethernet"),
    "AppGigabitEthernet": ("ap",),
    "Ethernet": ("eth", "et"),
    "Port-channel": ("po", "portchannel"),
    "Vlan": ("vl",),
    "Loopback": ("lo",),
}
INTF_NAMES = {}
for _full, _short in _INTF.items():
    INTF_NAMES[_full.lower()] = _full
    INTF_NAMES.update({s: _full for s in _short})

RE_INTF = re.compile(r"^([A-Za-z][A-Za-z-]*?)\s*(\d[\w/.:]*)$")
RE_MAC = re.compile(r"^(?:[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4}|[0-9a-f]{2}(?:[:-][0-9a-f]{2}){5}|[0-9a-f]{12})$", re.I)
RE_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def norm_intf(name: str) -> str:
    """Gi1/0/5 -> GigabitEthernet1/0/5, Eth1/5 -> Ethernet1/5. Unknown names come back unchanged."""
    name = name.strip()
    m = RE_INTF.match(name)
    full = INTF_NAMES.get(m.group(1).lower()) if m else None
    return full + m.group(2) if full else name


def norm_mac(s: str):
    """Dotted, colon, dash or bare hex -> 00:00:5e:00:53:10. None if it is not a MAC."""
    s = s.strip()
    if not RE_MAC.match(s):
        return None
    h = re.sub(r"[.:-]", "", s).lower()
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


def port_key(port: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", port)]


def ip_key(ip: str):
    return tuple(int(x) for x in ip.split("."))


def _lines(text: str) -> list:
    out = []
    for line in text.replace("\r", "").split("\n"):
        if "\x08" in line:  # --More-- erased with backspaces: keep what the terminal showed
            line = line.rsplit("\x08", 1)[1]
        if line.strip() == "--More--":
            continue
        out.append(line.expandtabs())
    return out


# ---------------------------------------------------------------- parsers

def parse_arp(text: str) -> list:
    """[(ip, mac, interface)] from IOS / IOS-XE or NX-OS 'show ip arp'. Incomplete entries are skipped."""
    out = []
    for line in _lines(text):
        t = line.split()
        if len(t) >= 4 and t[0] == "Internet" and RE_IPV4.match(t[1]):
            ip, mac, intf = t[1], norm_mac(t[3]), t[5] if len(t) > 5 else ""  # IOS
        elif len(t) >= 3 and RE_IPV4.match(t[0]):
            ip, mac, intf = t[0], norm_mac(t[2]), t[3] if len(t) > 3 else ""  # NX-OS
        else:
            continue
        if mac:
            out.append((ip, mac, norm_intf(intf)))
    return out


SKIP_PORTS = {"cpu", "router", "switch", "drop", "peer-link", "self"}


def parse_mac_table(text: str) -> list:
    """[(vlan, mac, port)] for unicast MACs learned on a port, from IOS / IOS-XE or NX-OS."""
    out = []
    for line in _lines(text):
        t = line.split()
        idx = next((i for i, x in enumerate(t) if norm_mac(x)), None)
        if idx is None or idx == 0 or len(t) < idx + 3 or not t[idx - 1].isdigit():
            continue  # no MAC, or VLAN is All / - (CPU and gateway entries)
        mac = norm_mac(t[idx])
        if int(mac[:2], 16) & 1:
            continue  # multicast or broadcast
        port = t[-1]
        if port.lower() in SKIP_PORTS or "(" in port or not any(c.isdigit() for c in port):
            continue
        port = norm_intf(port)
        if port.startswith(("Vlan", "sup-")):
            continue
        out.append((t[idx - 1], mac, port))
    return out


def parse_interfaces_status(text: str) -> dict:
    """{port: {description, status, vlan}} from 'show interfaces status', sliced by header positions."""
    out, cols = {}, None
    for line in _lines(text):
        t = line.split()
        if not t or set(line.strip()) <= set("-+= "):
            continue
        if t[0] == "Port" and "Status" in t and "Vlan" in t:
            cols = (line.find("Name"), line.find("Status"))
            continue
        if cols is None or "#" in t[0] or ">" in t[0] or not any(c.isdigit() for c in t[0]):
            continue
        name_at, status_at = cols
        if len(line) <= status_at:
            continue
        while status_at > 0 and line[status_at - 1] != " ":
            status_at -= 1  # value starts a little left of the header
        rest = line[status_at:].split()
        if len(rest) < 2:
            continue
        start = max(name_at, line.find(t[0]) + len(t[0]))
        desc = line[start:status_at].strip() if name_at >= 0 else ""
        out[norm_intf(t[0])] = {"description": "" if desc == "--" else desc,
                                "status": rest[0], "vlan": rest[1]}
    return out


@dataclass
class Neighbor:
    port: str            # local port, normalised
    name: str
    remote_port: str = ""
    platform: str = ""
    ip: str = ""
    infra: bool = False
    proto: str = ""


def parse_cdp(text: str) -> list:
    """Neighbours from 'show cdp neighbors detail' (IOS / IOS-XE and NX-OS)."""
    out = []
    blocks = re.split(r"(?m)^\s*Device ID\s*:", "\n".join(_lines(text)))
    for b in blocks[1:]:
        m = re.search(r"Interface:\s*([^,\n]+),\s*Port ID \(outgoing port\):\s*(.+)", b)
        if not m:
            continue
        sysname = re.search(r"(?m)^\s*System Name:\s*(\S+)", b)
        plat = re.search(r"Platform:\s*([^,\n]+)", b)
        ip = re.search(r"(?:IP address|IPv4 Address):\s*(\d+\.\d+\.\d+\.\d+)", b)
        caps = re.search(r"Capabilities:\s*(.*)", b)
        capset = set(caps.group(1).lower().split()) if caps else set()
        out.append(Neighbor(
            port=norm_intf(m.group(1)),
            name=sysname.group(1) if sysname else b.split("\n", 1)[0].strip(),
            remote_port=m.group(2).strip(),
            platform=plat.group(1).strip() if plat else "",
            ip=ip.group(1) if ip else "",
            infra=bool(capset & CDP_INFRA) and "phone" not in capset,
            proto="cdp",
        ))
    return out


def parse_lldp(text: str) -> list:
    """Neighbours from 'show lldp neighbors detail' (IOS / IOS-XE 'Local Intf', NX-OS 'Local Port id')."""
    recs, cur = [], {}
    for line in _lines(text):
        m = re.match(r"^\s*([A-Za-z][A-Za-z ]*?)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1).lower(), m.group(2).strip()
        if key in ("local intf", "chassis id") and key in cur:
            recs.append(cur)
            cur = {}
        if key in ("ip", "management address"):
            if RE_IPV4.match(val):
                cur.setdefault("mgmt_ip", val)
            continue
        cur.setdefault(key, val)
    if cur:
        recs.append(cur)

    out = []
    for r in recs:
        local = r.get("local intf") or r.get("local port id")
        if not local:
            continue
        caps = r.get("enabled capabilities") or r.get("system capabilities") or ""
        codes = {c.strip() for c in caps.split(",")}
        name = r.get("system name", "")
        if not name or name.startswith("-") or "not advertised" in name:
            name = r.get("chassis id", "")
        out.append(Neighbor(
            port=norm_intf(local), name=name, remote_port=r.get("port id", ""),
            platform=r.get("system description", "")[:60], ip=r.get("mgmt_ip", ""),
            infra=bool(codes & LLDP_INFRA) and "T" not in codes, proto="lldp",
        ))
    return out


# ---------------------------------------------------------------- vendor lookup

def load_oui(path) -> dict:
    """IEEE registry CSV (Registry,Assignment,Organization Name,...) -> {hex prefix: organisation}."""
    table = {}
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        r = csv.DictReader(fh)
        if not r.fieldnames or not {"Assignment", "Organization Name"} <= set(r.fieldnames):
            raise ValueError(f"{path}: needs the IEEE columns Assignment and Organization Name")
        for row in r:
            a = re.sub(r"[^0-9A-Fa-f]", "", row.get("Assignment") or "").upper()
            org = (row.get("Organization Name") or "").strip()
            if len(a) in (6, 7, 9) and org:
                table[a] = org
    return table


def vendor_of(mac: str, oui: dict) -> str:
    h = mac.replace(":", "").upper()
    return next((oui[h[:n]] for n in (9, 7, 6) if h[:n] in oui), "")


# ---------------------------------------------------------------- correlation

def build_switch(name: str, tables: dict, arp: dict, oui=None, threshold: int = 2):
    """tables: {kind: text}. arp: {mac: {ip}}. Returns (endpoint rows, infrastructure rows)."""
    status = parse_interfaces_status(tables.get("status", ""))
    neigh = {}
    for n in parse_cdp(tables.get("cdp", "")) + parse_lldp(tables.get("lldp", "")):
        neigh.setdefault(n.port, []).append(n)
    by_port = {}
    for vlan, mac, port in parse_mac_table(tables.get("mac", "")):
        by_port.setdefault(port, set()).add((vlan, mac))
    infra_ports = {p for p, ns in neigh.items() if any(n.infra for n in ns)}

    endpoints, infra = [], []
    for port in sorted(set(by_port) | infra_ports, key=port_key):
        st = status.get(port, {})
        entries = by_port.get(port, set())
        macs = {m for _, m in entries}
        mode = st.get("vlan", "").lower()
        reason = [mode] if mode in ("trunk", "routed") else []
        if not st and port.startswith("Port-channel"):
            reason.append("port-channel")
        infra_n = [n for n in neigh.get(port, []) if n.infra]
        reason += sorted({n.proto for n in infra_n})
        if reason:
            n = infra_n[0] if infra_n else Neighbor(port, "")
            infra.append({"switch": name, "port": port, "port_description": st.get("description", ""),
                          "vlan": st.get("vlan", ""), "reason": ";".join(reason), "neighbor": n.name,
                          "neighbor_port": n.remote_port, "neighbor_platform": n.platform,
                          "neighbor_ip": n.ip, "macs": len(macs)})
            continue
        for vlan, mac in sorted(entries, key=lambda e: (int(e[0]), e[1])):
            ips = ";".join(sorted(arp.get(mac, ()), key=ip_key))
            vendor = NO_OUI if oui is None else vendor_of(mac, oui)
            flags = []
            if len(macs) >= threshold:
                flags.append("multi-mac")
            if not ips:
                flags.append("no-ip")
            if oui is not None and not vendor:
                flags.append("unknown-vendor")
            if int(mac[:2], 16) & 2:
                flags.append("locally-administered")
            endpoints.append({"switch": name, "port": port, "port_description": st.get("description", ""),
                              "vlan": vlan, "mac": mac, "ip": ips, "vendor": vendor, "flags": ";".join(flags)})
    return endpoints, infra


def kind_of(path: Path):
    stem = re.sub(r"[- ]", "_", path.stem.lower())
    return next((k for prefix, k in FILE_KINDS if stem.startswith(prefix)), None)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def inventory(root, oui=None, threshold: int = 2) -> dict:
    """Build the inventory from a collect/ folder. Raises ValueError when there is nothing to build from."""
    root = Path(root)
    if not root.is_dir():
        raise ValueError(f"{root}: not a folder")
    arp_dir = root / "arp"
    arp_texts = []
    if arp_dir.is_dir():
        arp_texts += [_read(f) for f in sorted(arp_dir.iterdir()) if f.is_file() and not f.name.startswith(".")]

    switches = []
    for d in sorted(p for p in root.iterdir() if p.is_dir() and p.name != "arp" and not p.name.startswith(".")):
        tables = {}
        for f in sorted(d.iterdir()):
            k = kind_of(f) if f.is_file() else None
            if k:
                tables[k] = tables.get(k, "") + _read(f) + "\n"
        if "arp" in tables:
            arp_texts.append(tables["arp"])
        missing = [k for k in KINDS if k not in tables and not (k == "arp" and arp_dir.is_dir())]
        switches.append((d.name, tables, missing))
    if not switches:
        raise ValueError(f"{root}: no switch folders found")
    if not any("mac" in t for _, t, _ in switches):
        raise ValueError(f"{root}: no MAC address table found in any switch folder")

    arp, arp_entries = {}, 0
    for t in arp_texts:
        for ip, mac, _ in parse_arp(t):
            arp.setdefault(mac, set()).add(ip)
            arp_entries += 1

    result = {"endpoints": [], "infrastructure": [], "switches": [],
              "arp": {"entries": arp_entries, "files": len(arp_texts)}}
    for name, tables, missing in switches:
        ep, inf = build_switch(name, tables, arp, oui, threshold)
        result["endpoints"] += ep
        result["infrastructure"] += inf
        result["switches"].append({"switch": name, "ports_with_endpoints": len({r["port"] for r in ep}),
                                   "endpoints": len({r["mac"] for r in ep}),
                                   "infrastructure_links": len(inf), "missing": missing})
    return result


def summary(inv: dict) -> str:
    sw = inv["switches"]
    w = max([len("switch"), len("TOTAL")] + [len(s["switch"]) for s in sw])
    lines = [f"{'switch':<{w}}  {'ports':>5}  {'endpoints':>9}  {'infra links':>11}  missing files"]
    for s in sw:
        lines.append(f"{s['switch']:<{w}}  {s['ports_with_endpoints']:>5}  {s['endpoints']:>9}  "
                     f"{s['infrastructure_links']:>11}  {', '.join(s['missing']) or 'none'}")
    tot = lambda k: sum(s[k] for s in sw)
    lines.append(f"{'TOTAL':<{w}}  {tot('ports_with_endpoints'):>5}  {tot('endpoints'):>9}  "
                 f"{tot('infrastructure_links'):>11}")
    lines.append(f"\nARP: {inv['arp']['entries']} IP to MAC entries from {inv['arp']['files']} file(s).")
    counts = {}
    for r in inv["endpoints"]:
        for f in filter(None, r["flags"].split(";")):
            counts[f] = counts.get(f, 0) + 1
    order = ["multi-mac", "no-ip", "unknown-vendor", "locally-administered"]
    lines.append("Flags: " + (", ".join(f"{f} {counts[f]}" for f in order if f in counts) or "none"))
    if inv["infrastructure"]:
        lines.append("\nInfrastructure links (not in the endpoint list):")
        for r in inv["infrastructure"]:
            peer = " ".join(x for x in (r["neighbor"], r["neighbor_port"], r["neighbor_ip"]) if x)
            lines.append(f"  {r['switch']:<{w}}  {r['port']:<28} {r['reason']:<12} {peer or '(no neighbour seen)'}")
    return "\n".join(lines)


# ---------------------------------------------------------------- diff

def read_inventory_csv(path) -> list:
    with open(path, newline="", encoding="utf-8") as fh:
        r = csv.DictReader(fh)
        need = {"switch", "port", "vlan", "mac"}
        if not r.fieldnames or not need <= set(r.fieldnames):
            raise ValueError(f"{path}: not an inventory CSV (needs columns {', '.join(sorted(need))})")
        return list(r)


def diff(old_rows: list, new_rows: list) -> list:
    """NEW, GONE and MOVED (same MAC, different switch / port / VLAN) between two inventories."""
    def index(rows):
        idx = {}
        for r in rows:
            mac = norm_mac(r["mac"]) or r["mac"].strip().lower()
            e = idx.setdefault(mac, {"loc": set(), "ip": "", "vendor": ""})
            e["loc"].add((r["switch"].strip(), norm_intf(r["port"]), r["vlan"].strip()))
            e["ip"] = e["ip"] or (r.get("ip") or "").strip()
            e["vendor"] = e["vendor"] or (r.get("vendor") or "").strip()
        return idx

    old, new = index(old_rows), index(new_rows)
    changes = []
    for mac in sorted(set(old) | set(new)):
        o, n = old.get(mac), new.get(mac)
        if o is None:
            change = "NEW"
        elif n is None:
            change = "GONE"
        elif o["loc"] != n["loc"]:
            change = "MOVED"
        else:
            continue
        row = {"change": change, "mac": mac, "ip": (n or o)["ip"], "vendor": (n or o)["vendor"]}
        for side, e in (("old", o), ("new", n)):
            locs = sorted(e["loc"]) if e else []
            for i, col in enumerate(("switch", "port", "vlan")):
                row[f"{side}_{col}"] = ";".join(l[i] for l in locs)
        changes.append(row)
    return changes


def _where(row, side):
    if not row[f"{side}_switch"]:
        return "-"
    parts = zip(*(row[f"{side}_{c}"].split(";") for c in ("switch", "port", "vlan")))
    return "; ".join(f"{s} {p} vlan {v}" for s, p, v in parts)


# ---------------------------------------------------------------- CLI

def _write_csv(path, fields, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def _main_diff(argv) -> int:
    p = argparse.ArgumentParser(prog="ot-inventory diff",
                                description="Compare two inventory CSVs: NEW, GONE and MOVED MACs")
    p.add_argument("old", help="earlier inventory CSV")
    p.add_argument("new", help="later inventory CSV")
    p.add_argument("--csv", help="also write the changes to this CSV file")
    a = p.parse_args(argv)
    try:
        changes = diff(read_inventory_csv(a.old), read_inventory_csv(a.new))
        if a.csv:
            _write_csv(a.csv, DIFF_FIELDS, changes)
    except (OSError, ValueError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not changes:
        print("No changes.")
        return 0
    print(f"{'CHANGE':<6}  {'MAC':<17}  {'IP':<15}  OLD -> NEW")
    for c in changes:
        print(f"{c['change']:<6}  {c['mac']:<17}  {c['ip'] or '-':<15}  {_where(c, 'old')} -> {_where(c, 'new')}")
    n = {k: sum(1 for c in changes if c["change"] == k) for k in ("NEW", "GONE", "MOVED")}
    print(f"\n{len(changes)} changes: {n['NEW']} new, {n['GONE']} gone, {n['MOVED']} moved.")
    return 1


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] == "diff":
        return _main_diff(argv[1:])
    p = argparse.ArgumentParser(
        prog="ot-inventory",
        description="Passive OT asset inventory from saved switch show-command output",
        epilog="Diff mode: ot-inventory diff old.csv new.csv [--csv changes.csv]")
    p.add_argument("folder", help="collect folder: one subfolder per switch, optional arp/ subfolder")
    p.add_argument("--oui", help="IEEE MA-L registry CSV for the vendor column")
    p.add_argument("--csv", help="write endpoints to this CSV file")
    p.add_argument("--json", help="write endpoints, infrastructure links and summary to this JSON file")
    p.add_argument("--multi-mac-threshold", type=int, default=2, metavar="N",
                   help="flag access ports with N or more MACs (default 2)")
    a = p.parse_args(argv)
    if a.multi_mac_threshold < 2:
        print("error: --multi-mac-threshold must be 2 or more", file=sys.stderr)
        return 2
    try:
        oui = load_oui(a.oui) if a.oui else None
        inv = inventory(a.folder, oui, a.multi_mac_threshold)
        if a.csv:
            _write_csv(a.csv, FIELDS, inv["endpoints"])
        if a.json:
            Path(a.json).write_text(json.dumps(inv, indent=2), encoding="utf-8")
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(summary(inv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
