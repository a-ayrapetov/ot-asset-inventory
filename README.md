# ot-asset-inventory

[![ci](https://github.com/a-ayrapetov/ot-asset-inventory/actions/workflows/ci.yml/badge.svg)](https://github.com/a-ayrapetov/ot-asset-inventory/actions/workflows/ci.yml)

Passive OT asset inventory built from the tables your switches already keep. Save read-only show command output from each switch into its own folder and get one row per device: switch, port, port description, VLAN, MAC, IP, vendor and flags that point at hidden switches, layer 2 only devices and randomized MACs. It correlates the ARP table (IP to MAC), the MAC address table (MAC to port and VLAN), `show interfaces status` (port to VLAN, status and description) and CDP or LLDP detail (port to neighbour). Not a single packet goes to a PLC, drive, HMI, camera or controller.

```bash
pip install git+https://github.com/a-ayrapetov/ot-asset-inventory
ot-inventory collect/ --oui oui.csv --csv inventory.csv --json inventory.json
```

## Usage

```bash
ot-inventory collect/
ot-inventory collect/ --csv inventory.csv
ot-inventory collect/ --oui oui.csv --csv inventory.csv --multi-mac-threshold 3
ot-inventory diff inventory-old.csv inventory.csv --csv changes.csv
```

You can also run `python ot_inventory.py` straight from a clone. Example against the included fixtures:

```
switch      ports  endpoints  infra links  missing files
access-sw1      5          7            1  lldp
nx-sw2          2          2            3  cdp
TOTAL           7          9            4

ARP: 9 IP to MAC entries from 2 file(s).
Flags: multi-mac 3, no-ip 3, unknown-vendor 1, locally-administered 2

Infrastructure links (not in the endpoint list):
  access-sw1  TenGigabitEthernet1/1/1      trunk;cdp    core-sw1 TwentyFiveGigE1/0/11 198.51.100.1
  nx-sw2      Ethernet1/4                  lldp         cell9-sw port 2 192.0.2.54
  nx-sw2      Ethernet1/49                 trunk;lldp   core-sw1 TwentyFiveGigE1/0/12 198.51.100.1
  nx-sw2      Port-channel10               trunk        (no neighbour seen)
```

## Input folder layout

```
collect/
  access-sw1/                          one folder per switch, folder name = switch name
    show_ip_arp.txt                    or show_arp.txt
    show_mac_address-table.txt
    show_interfaces_status.txt
    show_cdp_neighbors_detail.txt
    show_lldp_neighbors_detail.txt
  nx-sw2/
    ...
  arp/                                 optional: ARP output from the layer 3 device, any file names
    core-sw1_vrf_default.txt
    core-sw1_vrf_ot.txt
```

Every file is optional. The summary lists the missing ones as `arp`, `mac`, `status`, `cdp` and `lldp`. File names are matched on how they start, ignoring case and treating `-` and `_` alike, so `show_ip_arp_vrf_ot.txt` counts as ARP and two files of one kind in a folder are read together. Every ARP file found, in a switch folder or in `arp/`, goes into one global IP to MAC map, because the ARP table usually lives on the layer 3 core and not on the access switch. When `arp/` exists, a switch folder without its own ARP file is not reported as missing it.

The parser was written against common IOS-XE and NX-OS output of `show ip arp`, `show mac address-table`, `show interfaces status` (`show interface status` on NX-OS), `show cdp neighbors detail` and `show lldp neighbors detail`. CLI formats vary by platform and release, so run it on one switch first and check the result against the raw output.

## Output

| Column | Meaning |
|---|---|
| switch | switch folder name |
| port | interface in full form: `Gi1/0/5` is written `GigabitEthernet1/0/5`, `Eth1/5` is written `Ethernet1/5` |
| port_description | Name column of `show interfaces status` |
| vlan | VLAN the MAC was learned in |
| mac | colon separated, lower case |
| ip | every IP the ARP tables hold for that MAC, `;` separated, empty when there is none |
| vendor | organisation from the IEEE registry, `(no OUI file)` when `--oui` is not given |
| flags | `;` separated, see below |

| Flag | Meaning |
|---|---|
| `multi-mac` | access port with 2 or more MACs (tune with `--multi-mac-threshold`): a hidden unmanaged switch, a hub, or a device with a built-in switch such as a phone or a ring-capable PLC |
| `no-ip` | MAC with no ARP entry: a layer 2 only device, or its ARP table lives on a router you did not collect |
| `unknown-vendor` | `--oui` was given but the MAC prefix is not in it |
| `locally-administered` | second-lowest bit of the first octet is set: a randomized or virtual MAC, so the vendor lookup tells you nothing |

Trunk ports, port-channels when no status file says otherwise, and ports whose CDP or LLDP neighbour advertises router, switch, bridge, access point or repeater capability are kept out of the endpoint list and reported as infrastructure links. Phones stay in the endpoint list. An OT device with a built-in switch that advertises Bridge in LLDP will land on the infrastructure list, so read that list too. Multicast, broadcast, CPU and router MACs are skipped.

The JSON file holds `endpoints` (the CSV rows), `infrastructure` (switch, port, reason, neighbour name, port, platform and IP, MAC count) and a per switch `switches` summary.

For the vendor column, download the MA-L registry CSV from the IEEE (`https://standards-oui.ieee.org/oui/oui.csv`, columns `Registry,Assignment,Organization Name,Organization Address`) and pass it with `--oui`. No OUI data is bundled. MA-M and MA-S registry files work too, and the longest matching prefix wins.

## Collecting the input safely

These are the commands, run on the switches only:

```
terminal length 0
show ip arp
show ip arp vrf <name>          IOS-XE: once per VRF
show mac address-table
show interfaces status          NX-OS: show interface status
show cdp neighbors detail
show lldp neighbors detail
```

- Use read-only show commands on switches and nothing else. Nothing is sent to the OT endpoints.
- No ping sweeps and no scans on OT VLANs. The whole point is that the endpoints never see you.
- Never poll safety systems, interlocks or serial-to-Ethernet converters. They are endpoints, not switches.
- Throttle collection: a handful of concurrent sessions and a pause between commands, so switch CPUs and slow management links stay quiet.
- Poll no more often than every 15 minutes. Hourly is a sensible default.
- Start with two known-good switches and check the result against what is physically on those ports before you widen the run.
- Keep every raw output next to the parsed result, so any row can be traced back to the line it came from.
- On IOS-XE the ARP table is per VRF. Collect `show ip arp vrf <name>` for each VRF that carries OT traffic, or those devices show up as `no-ip`.

## Why this exists

On industrial networks with thousands of switches, the device list in the monitoring system was dirty and active scanning was off the table, because PLCs, drives and older controllers can fall over when probed. The switches already knew what was plugged into every port: they keep MAC, ARP and neighbour tables as a by-product of forwarding. Reading those tables became the inventory. This tool is that method, packaged.

## Diff mode

```bash
ot-inventory diff inventory-old.csv inventory.csv
ot-inventory diff inventory-old.csv inventory.csv --csv changes.csv
```

Compares two inventory CSVs by MAC and reports NEW MACs, GONE MACs and MOVED MACs (same MAC, different switch, port or VLAN). Port names are normalised before comparing, so `Gi1/0/5` and `GigabitEthernet1/0/5` are the same port. Run the inventory on a schedule, keep the previous CSV and diff the two.

## Exit codes

`ot-inventory <folder>`: `0` inventory built, `2` error (folder missing, no switch folders, no MAC address table anywhere, unreadable OUI file).

`ot-inventory diff`: `0` no changes, `1` changes found, `2` error, so it runs cleanly from cron or a scheduled task.

## Safety

Text parsing only, standard library only, never connects to anything: not to the switches, not to the endpoints, not to the internet. It reads the files you point it at and writes only the output files you name. The output is an asset list of an industrial network, so treat it as sensitive. `.gitignore` excludes `*.csv`, `*.json` and a top level `collect/` folder to keep real data out of a clone.

## Tests

```bash
pip install pytest && python -m pytest
```

Fixtures use RFC 7042 documentation MACs, locally administered MACs and RFC 5737 documentation IPs only.

## License

MIT. See LICENSE. Security reports: see SECURITY.md.
