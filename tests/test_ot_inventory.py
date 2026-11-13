import csv
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import ot_inventory as ot  # noqa: E402

FX = HERE / "fixtures"
COLLECT = FX / "collect"
OUI = FX / "oui.csv"


def fixture(*parts):
    return (COLLECT.joinpath(*parts)).read_text()


def rows_by_mac(inv):
    return {r["mac"]: r for r in inv["endpoints"]}


# ---------------------------------------------------------------- normalisation

def test_mac_forms_normalise_to_colon_lower():
    for raw in ("0000.5e00.5310", "00:00:5E:00:53:10", "00-00-5e-00-53-10", "00005E005310"):
        assert ot.norm_mac(raw) == "00:00:5e:00:53:10", raw
    for bad in ("Incomplete", "INCOMPLETE", "00:03:12", "192.0.2.10", "Gi1/0/5"):
        assert ot.norm_mac(bad) is None, bad


def test_interface_names_normalise():
    cases = {
        "Gi1/0/5": "GigabitEthernet1/0/5", "GigabitEthernet1/0/5": "GigabitEthernet1/0/5",
        "Eth1/5": "Ethernet1/5", "Ethernet1/5": "Ethernet1/5", "Te1/1/1": "TenGigabitEthernet1/1/1",
        "Fa0/1": "FastEthernet0/1", "Twe1/0/11": "TwentyFiveGigE1/0/11", "Tw1/0/1": "TwoGigabitEthernet1/0/1",
        "Hu1/0/49": "HundredGigE1/0/49", "Fo1/1/1": "FortyGigabitEthernet1/1/1",
        "Po10": "Port-channel10", "port-channel10": "Port-channel10", "Gi 1/0/5": "GigabitEthernet1/0/5",
        "mgmt0": "mgmt0", "Port 1": "Port 1",
    }
    for raw, want in cases.items():
        assert ot.norm_intf(raw) == want, raw


# ---------------------------------------------------------------- parsers

def test_ios_arp_skips_incomplete():
    arp = ot.parse_arp(fixture("arp", "core-sw1_show_ip_arp.txt"))
    assert len(arp) == 7
    assert ("192.0.2.11", "00:00:5e:00:53:01", "Vlan10") in arp
    assert not any(ip == "192.0.2.13" for ip, _, _ in arp)


def test_nxos_arp_skips_incomplete_and_trailing_flags():
    arp = ot.parse_arp(fixture("nx-sw2", "show_ip_arp.txt"))
    assert arp == [("192.0.2.51", "00:00:5e:00:53:51", "Vlan40"), ("192.0.2.1", "00:00:5e:00:53:f1", "Vlan40")]


def test_ios_mac_table_skips_cpu_all_and_multicast():
    macs = ot.parse_mac_table(fixture("access-sw1", "show_mac_address-table.txt"))
    assert len(macs) == 9
    assert ("10", "00:00:5e:00:53:02", "GigabitEthernet1/0/2") in macs  # sticky STATIC on a port is kept
    assert not any(m.startswith("03:") or m == "ff:ff:ff:ff:ff:ff" for _, m, _ in macs)
    assert not any(m == "00:00:5e:00:53:a0" for _, m, _ in macs)  # STATIC to CPU
    assert sum(1 for _, _, p in macs if p == "GigabitEthernet1/0/3") == 3


def test_nxos_mac_table_markers_peer_link_and_router_mac():
    macs = ot.parse_mac_table(fixture("nx-sw2", "show_mac_address-table.txt"))
    ports = {p for _, _, p in macs}
    assert ports == {"Ethernet1/1", "Ethernet1/2", "Ethernet1/4", "Ethernet1/49", "Port-channel10"}
    assert ("40", "02:00:00:00:00:52", "Ethernet1/2") in macs
    assert not any(m in ("00:00:5e:00:53:60", "00:00:5e:00:53:e0") for _, m, _ in macs)


def test_ios_interfaces_status_description_with_spaces():
    st = ot.parse_interfaces_status(fixture("access-sw1", "show_interfaces_status.txt"))
    assert st["GigabitEthernet1/0/1"] == {"description": "PLC LINE 3 CAB A", "status": "connected", "vlan": "10"}
    assert st["GigabitEthernet1/0/4"]["description"] == ""
    assert st["GigabitEthernet1/0/5"]["status"] == "notconnect"
    assert st["TenGigabitEthernet1/1/1"]["vlan"] == "trunk"
    assert len(st) == 8


def test_status_value_left_of_header_is_tolerated():
    text = ("Port      Name               Status       Vlan       Duplex  Speed Type\n"
            "Gi1/0/9   Pump 4            err-disabled 10           auto   auto 10/100/1000BaseTX\n")
    assert ot.parse_interfaces_status(text)["GigabitEthernet1/0/9"] == {
        "description": "Pump 4", "status": "err-disabled", "vlan": "10"}


def test_nxos_interface_status_dashes_and_routed():
    st = ot.parse_interfaces_status(fixture("nx-sw2", "show_interfaces_status.txt"))
    assert st["mgmt0"] == {"description": "", "status": "connected", "vlan": "routed"}
    assert st["Ethernet1/1"]["description"] == "Camera yard 1"
    assert st["Ethernet1/2"]["description"] == ""
    assert st["Port-channel10"]["vlan"] == "trunk"


def test_ios_cdp_detail():
    n = {x.port: x for x in ot.parse_cdp(fixture("access-sw1", "show_cdp_neighbors_detail.txt"))}
    core = n["TenGigabitEthernet1/1/1"]
    assert (core.name, core.remote_port, core.ip, core.infra) == ("core-sw1", "TwentyFiveGigE1/0/11", "198.51.100.1", True)
    assert core.platform == "cisco C9500-48Y4C"
    phone = n["GigabitEthernet1/0/4"]
    assert phone.infra is False and phone.remote_port == "Port 1" and phone.ip == "203.0.113.41"


NXOS_CDP = """\
----------------------------------------
Device ID:dist-sw3(SN000000001)
System Name: dist-sw3

Interface address(es):
    IPv4 Address: 198.51.100.3
Platform: N9K-C93180YC-EX, Capabilities: Router Switch IGMP Filtering Supports-STP-Dispute
Interface: Ethernet1/50, Port ID (outgoing port): Ethernet1/50
Holdtime: 140 sec
"""


def test_nxos_cdp_detail():
    (n,) = ot.parse_cdp(NXOS_CDP)
    assert (n.port, n.name, n.ip, n.remote_port, n.infra) == ("Ethernet1/50", "dist-sw3", "198.51.100.3", "Ethernet1/50", True)


def test_cdp_phone_with_switch_capability_stays_endpoint():
    text = ("Device ID: phone-9\nPlatform: IP Phone,  Capabilities: Host Phone Switch \n"
            "Interface: GigabitEthernet1/0/9,  Port ID (outgoing port): Port 1\n")
    (n,) = ot.parse_cdp(text)
    assert n.infra is False


IOS_LLDP = """\
------------------------------------------------
Local Intf: Gi1/0/48
Chassis id: 0000.5e00.53f2
Port id: Gi1/0/1
Port Description: GigabitEthernet1/0/1
System Name: dist-sw2

System Description:
Cisco IOS Software [Cupertino], Catalyst L3 Switch Software

Time remaining: 101 seconds
System Capabilities: B,R
Enabled Capabilities: B,R
Management Addresses:
    IP: 198.51.100.2
Auto Negotiation - supported, enabled
Vlan ID: - not advertised

------------------------------------------------
Local Intf: Gi1/0/7
Chassis id: 0000.5e00.5307
Port id: 0000.5e00.5307
System Name: phone-7
Time remaining: 160 seconds
System Capabilities: B,T
Enabled Capabilities: B,T
Management Addresses:
    IP: 192.0.2.7

Total entries displayed: 2
"""


def test_ios_lldp_detail():
    n = {x.port: x for x in ot.parse_lldp(IOS_LLDP)}
    up = n["GigabitEthernet1/0/48"]
    assert (up.name, up.remote_port, up.ip, up.infra) == ("dist-sw2", "Gi1/0/1", "198.51.100.2", True)
    assert n["GigabitEthernet1/0/7"].infra is False  # telephone with a bridge is still an endpoint


def test_nxos_lldp_detail():
    n = {x.port: x for x in ot.parse_lldp(fixture("nx-sw2", "show_lldp_neighbors_detail.txt"))}
    assert set(n) == {"Ethernet1/1", "Ethernet1/4", "Ethernet1/49"}
    assert n["Ethernet1/1"].infra is False and n["Ethernet1/1"].ip == "192.0.2.51"
    assert n["Ethernet1/4"].infra is True and n["Ethernet1/49"].name == "core-sw1"


# ---------------------------------------------------------------- inventory

def test_endpoint_rows_and_flags():
    inv = ot.inventory(COLLECT, ot.load_oui(OUI))
    r = rows_by_mac(inv)
    assert set(r) == {"00:00:5e:00:53:01", "00:00:5e:00:53:02", "00:00:5e:00:53:31", "00:00:5e:00:53:32",
                      "00:00:5e:00:53:33", "00:00:5e:00:53:41", "02:00:ab:00:00:06", "00:00:5e:00:53:51",
                      "02:00:00:00:00:52", "00:00:5e:00:53:54", "00:00:5e:00:53:55"}
    assert r["00:00:5e:00:53:01"] == {"switch": "access-sw1", "port": "GigabitEthernet1/0/1",
                                      "port_description": "PLC LINE 3 CAB A", "vlan": "10",
                                      "mac": "00:00:5e:00:53:01", "ip": "192.0.2.11",
                                      "vendor": "Example Automation Ltd", "flags": ""}
    assert r["00:00:5e:00:53:31"]["flags"] == "multi-mac"
    assert r["00:00:5e:00:53:33"]["flags"] == "multi-mac;no-ip"
    assert r["02:00:ab:00:00:06"]["vendor"] == "Sample Drives Co"
    assert r["02:00:ab:00:00:06"]["flags"] == "no-ip;locally-administered"
    assert r["02:00:00:00:00:52"]["flags"] == "no-ip;unknown-vendor;locally-administered"
    assert r["00:00:5e:00:53:41"]["flags"] == ""  # phone CDP neighbour does not make it infrastructure


def test_ip_comes_from_separate_l3_arp_folder():
    inv = ot.inventory(COLLECT)
    assert rows_by_mac(inv)["00:00:5e:00:53:31"]["ip"] == "198.51.100.31"
    assert inv["arp"] == {"entries": 9, "files": 2}


def test_trunks_are_excluded_and_bridge_ports_kept():
    inv = ot.inventory(COLLECT)
    ports = {(r["switch"], r["port"]) for r in inv["endpoints"]}
    infra = {(r["switch"], r["port"]): r["reason"] for r in inv["infrastructure"]}
    assert infra == {("access-sw1", "TenGigabitEthernet1/1/1"): "trunk;cdp", ("nx-sw2", "Ethernet1/4"): "lldp",
                     ("nx-sw2", "Ethernet1/49"): "trunk;lldp", ("nx-sw2", "Port-channel10"): "trunk"}
    assert ports & set(infra) == {("nx-sw2", "Ethernet1/4")}  # access port behind a bridge
    assert "00:00:5e:00:53:f1" not in rows_by_mac(inv)  # learned on trunks only
    assert "00:00:5e:00:53:70" not in rows_by_mac(inv)  # learned on a port-channel


def test_devices_behind_a_bridge_stay_in_the_list():
    r = rows_by_mac(ot.inventory(COLLECT, ot.load_oui(OUI)))
    for mac in ("00:00:5e:00:53:54", "00:00:5e:00:53:55"):
        assert (r[mac]["switch"], r[mac]["port"], r[mac]["vlan"]) == ("nx-sw2", "Ethernet1/4", "40")
        assert r[mac]["port_description"] == "Cell 9 ring sw"
        assert r[mac]["flags"] == "behind-bridge;no-ip"  # no multi-mac: the bridge is already known


def test_bridge_neighbour_on_port_of_unknown_mode_stays_out(tmp_path):
    sw = tmp_path / "nx-sw2"
    sw.mkdir()
    for f in ("show_mac_address-table.txt", "show_lldp_neighbors_detail.txt"):
        shutil.copy(COLLECT / "nx-sw2" / f, sw / f)
    inv = ot.inventory(tmp_path)
    ports = {r["port"] for r in inv["endpoints"]}
    assert ports == {"Ethernet1/1", "Ethernet1/2"}  # without status an uplink cannot be told from an access port
    assert {r["port"] for r in inv["infrastructure"]} == {"Ethernet1/4", "Ethernet1/49", "Port-channel10"}


def test_several_mac_captures_are_read_as_a_union(tmp_path):
    sw = tmp_path / "access-sw1"
    shutil.copytree(COLLECT / "access-sw1", sw)
    (sw / "show_mac_address-table_0900.txt").write_text(
        "Vlan    Mac Address       Type        Ports\n"
        "  10    0000.5e00.5305    DYNAMIC     Gi1/0/5\n"
        "  10    0000.5e00.5301    DYNAMIC     Gi1/0/1\n")
    r = rows_by_mac(ot.inventory(tmp_path))
    assert r["00:00:5e:00:53:05"]["port"] == "GigabitEthernet1/0/5"  # only in the second capture
    assert "00:00:5e:00:53:31" in r  # only in the first capture
    assert sum(1 for x in r.values() if x["mac"] == "00:00:5e:00:53:01") == 1  # seen twice, listed once


def test_vendor_without_oui_file():
    inv = ot.inventory(COLLECT)
    assert {r["vendor"] for r in inv["endpoints"]} == {ot.NO_OUI}
    assert not any("unknown-vendor" in r["flags"] for r in inv["endpoints"])


def test_joined_oui_file_longest_prefix_wins(tmp_path):
    mac = "00:00:5e:00:53:01"
    ma_m, ma_s = "MA-M,00005E0,Sample Sensors Ltd,x\n", "MA-S,00005E005,Sample Valves Co,x\n"
    joined = tmp_path / "joined.csv"
    joined.write_text(OUI.read_text() + ma_m)
    assert ot.vendor_of(mac, ot.load_oui(joined)) == "Sample Sensors Ltd"  # MA-M beats MA-L
    joined.write_text(OUI.read_text() + ma_m + ma_s)
    assert ot.vendor_of(mac, ot.load_oui(joined)) == "Sample Valves Co"  # MA-S beats MA-M
    assert ot.vendor_of(mac, ot.load_oui(OUI)) == "Example Automation Ltd"


def test_multi_mac_threshold():
    at = ot.inventory(COLLECT, threshold=3)  # Gi1/0/3 has exactly 3 MACs
    assert sum("multi-mac" in r["flags"] for r in at["endpoints"]) == 3
    above = ot.inventory(COLLECT, threshold=4)
    assert not any("multi-mac" in r["flags"] for r in above["endpoints"])


def test_missing_files_reported(tmp_path):
    inv = ot.inventory(COLLECT)
    missing = {s["switch"]: s["missing"] for s in inv["switches"]}
    assert missing == {"access-sw1": ["lldp"], "nx-sw2": ["cdp"]}  # ARP covered by arp/

    sw = tmp_path / "sw9"
    sw.mkdir()
    shutil.copy(COLLECT / "access-sw1" / "show_mac_address-table.txt", sw / "show_mac_address-table.txt")
    inv = ot.inventory(tmp_path)
    assert inv["switches"][0]["missing"] == ["arp", "status", "cdp", "lldp"]
    assert all("no-ip" in r["flags"] for r in inv["endpoints"])
    assert "missing files" in ot.summary(inv)


def test_port_channel_is_uplink_when_status_is_missing(tmp_path):
    sw = tmp_path / "nx-sw9"
    sw.mkdir()
    shutil.copy(COLLECT / "nx-sw2" / "show_mac_address-table.txt", sw / "show_mac_address-table.txt")
    inv = ot.inventory(tmp_path)
    assert {r["port"]: r["reason"] for r in inv["infrastructure"]} == {"Port-channel10": "port-channel"}
    assert "Port-channel10" not in {r["port"] for r in inv["endpoints"]}


def test_summary_totals():
    text = ot.summary(ot.inventory(COLLECT, ot.load_oui(OUI)))
    table = text.split("\n\n")[0].splitlines()
    assert table[0].split()[:4] == ["switch", "macs", "ports", "endpoints"]
    rows = {l.split()[0]: l.split()[1:5] for l in table[1:]}
    assert rows == {"access-sw1": ["8", "5", "7", "1"], "nx-sw2": ["6", "3", "4", "3"], "TOTAL": ["14", "8", "11", "4"]}
    assert "Flags: multi-mac 3, behind-bridge 2, no-ip 5, unknown-vendor 1, locally-administered 2" in text
    assert "Port-channel10" in text


# ---------------------------------------------------------------- diff

def _row(mac, switch="access-sw1", port="GigabitEthernet1/0/1", vlan="10"):
    return {"switch": switch, "port": port, "port_description": "", "vlan": vlan, "mac": mac,
            "ip": "", "vendor": "", "flags": ""}


def test_diff_new_gone_moved():
    old = [_row("00:00:5e:00:53:01"), _row("00:00:5e:00:53:02", port="Gi1/0/2"), _row("00:00:5e:00:53:03")]
    new = [_row("00:00:5e:00:53:01"), _row("00:00:5e:00:53:02", port="GigabitEthernet1/0/2"),
           _row("00:00:5e:00:53:03", port="GigabitEthernet1/0/9"), _row("02:00:00:00:00:99")]
    old.remove(old[0])
    ch = {c["mac"]: c for c in ot.diff(old, new)}
    assert ch["00:00:5e:00:53:01"]["change"] == "NEW"
    assert ch["02:00:00:00:00:99"]["change"] == "NEW"
    assert ch["00:00:5e:00:53:03"]["change"] == "MOVED"
    assert ch["00:00:5e:00:53:03"]["new_port"] == "GigabitEthernet1/0/9"
    assert "00:00:5e:00:53:02" not in ch  # Gi1/0/2 and GigabitEthernet1/0/2 are the same port
    assert ot.diff(new, old)[0]["change"] == "GONE"


# ---------------------------------------------------------------- CLI

def test_cli_collect_outputs_and_exit_codes(tmp_path, capsys):
    out_csv, out_json = tmp_path / "inv.csv", tmp_path / "inv.json"
    assert ot.main([str(COLLECT), "--oui", str(OUI), "--csv", str(out_csv), "--json", str(out_json)]) == 0
    with open(out_csv, newline="") as fh:
        r = csv.DictReader(fh)
        assert r.fieldnames == ot.FIELDS
        assert len(list(r)) == 11
    data = json.loads(out_json.read_text())
    assert len(data["endpoints"]) == 11 and len(data["infrastructure"]) == 4
    assert data["switches"][1]["macs"] == 6
    assert "access-sw1" in capsys.readouterr().out

    assert ot.main([str(tmp_path / "nope")]) == 2
    empty = tmp_path / "empty"
    empty.mkdir()
    assert ot.main([str(empty)]) == 2
    assert ot.main([str(COLLECT), "--multi-mac-threshold", "1"]) == 2
    bad_oui = tmp_path / "bad.csv"
    bad_oui.write_text("a,b\n1,2\n")
    assert ot.main([str(COLLECT), "--oui", str(bad_oui)]) == 2


def test_cli_diff_exit_codes(tmp_path):
    a, b, changes = tmp_path / "a.csv", tmp_path / "b.csv", tmp_path / "changes.csv"
    assert ot.main([str(COLLECT), "--csv", str(a)]) == 0
    assert ot.main(["diff", str(a), str(a)]) == 0

    rows = list(csv.DictReader(open(a, newline="")))
    rows[0]["port"] = "GigabitEthernet1/0/5"   # moved
    gone = rows.pop(1)                          # gone
    rows.append(dict(gone, mac="02:00:00:00:00:77"))  # new
    with open(b, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=ot.FIELDS)
        w.writeheader()
        w.writerows(rows)
    assert ot.main(["diff", str(a), str(b), "--csv", str(changes)]) == 1
    got = {r["change"] for r in csv.DictReader(open(changes, newline=""))}
    assert got == {"NEW", "GONE", "MOVED"}

    assert ot.main(["diff", str(a), str(tmp_path / "missing.csv")]) == 2
    junk = tmp_path / "junk.csv"
    junk.write_text("x,y\n1,2\n")
    assert ot.main(["diff", str(a), str(junk)]) == 2
