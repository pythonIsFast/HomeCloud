"""Host networking for microVMs: one tap device and one /30 link per VM.

Addressing is *derived from the resource id*, not allocated:

    offset  = resource_id * 4
    network = <prefix>.<offset // 256>.<offset % 256>/30
    tap     = network + 1      (host side)
    guest   = network + 2      (VM side)

That means there is no lease table, no allocation lock and no chance of two
VMs racing for the same address -- the id already is the reservation. With the
default 10.71.0.0/16 prefix that is 16 383 concurrent VMs per host, which is
far beyond what one box will run.

The guest gets its address from the kernel command line (see firecracker.py),
so there is no DHCP server to operate.

Everything in this module shells out to ``ip`` and ``iptables``. Those are base
system tools, not Python packages, so the dependency policy is unaffected --
but they do require root, which is why only the worker calls this.
"""

import ipaddress
import re
import shutil
import subprocess

# 4 addresses per VM: network, host, guest, broadcast.
BLOCK_SIZE = 4
MAX_RESOURCE_ID = (65536 // BLOCK_SIZE) - 1


class NetworkError(Exception):
    pass


def _run(args, check=True):
    """Run a host command and return (returncode, output)."""
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=20, check=False
    )
    output = (result.stdout + result.stderr).strip()
    if check and result.returncode != 0:
        raise NetworkError(f"{' '.join(args)} failed: {output}")
    return result.returncode, output


def plan(resource_id, prefix="10.71"):
    """Return the deterministic network plan for one VM. Pure function."""
    resource_id = int(resource_id)
    if not 1 <= resource_id <= MAX_RESOURCE_ID:
        raise NetworkError(
            f"resource id {resource_id} outside addressable range 1..{MAX_RESOURCE_ID}"
        )

    offset = resource_id * BLOCK_SIZE
    network = ipaddress.IPv4Network(
        f"{prefix}.{offset // 256}.{offset % 256}/30", strict=True
    )
    hosts = list(network.hosts())  # exactly two for a /30

    host_ip, guest_ip = str(hosts[0]), str(hosts[1])
    octets = guest_ip.split(".")
    return {
        "tap": f"hc-vm{resource_id}",
        "network": str(network),
        "netmask": str(network.netmask),
        "host_ip": host_ip,
        "guest_ip": guest_ip,
        # Locally administered MAC (02 in the first octet would also do); the
        # last four bytes are the guest address, so it is unique per VM.
        "mac": "06:00:%02x:%02x:%02x:%02x" % tuple(int(o) for o in octets),
    }


def default_egress_interface():
    """The interface the host routes to the internet through, e.g. 'eth0'."""
    _, output = _run(["ip", "-4", "route", "show", "default"])
    match = re.search(r"\bdev\s+(\S+)", output)
    if not match:
        raise NetworkError("no default route found; set HOMECLOUD_VM_EGRESS_IF")
    return match.group(1)


def require_tools():
    """Fail early and clearly instead of half-way through a VM creation."""
    missing = [tool for tool in ("ip", "iptables") if shutil.which(tool) is None]
    if missing:
        raise NetworkError(f"missing host tools: {', '.join(missing)}")


def enable_forwarding():
    """Turn on IPv4 forwarding; without it the VM has no route off the host."""
    with open("/proc/sys/net/ipv4/ip_forward", "r+", encoding="ascii") as handle:
        if handle.read().strip() != "1":
            handle.write("1")


def ensure_nat(prefix, egress_interface):
    """One MASQUERADE rule for the whole VM range, added once.

    -C checks first, so calling this on every worker start does not stack up
    duplicate rules.
    """
    source = f"{prefix}.0.0/16"
    check = ["iptables", "-t", "nat", "-C", "POSTROUTING",
             "-s", source, "-o", egress_interface, "-j", "MASQUERADE"]
    code, _ = _run(check, check=False)
    if code != 0:
        _run(["iptables", "-t", "nat", "-A", "POSTROUTING",
              "-s", source, "-o", egress_interface, "-j", "MASQUERADE"])

    # Allow forwarding both ways for the VM range.
    for rule in (
        ["-s", source, "-o", egress_interface],
        ["-d", source, "-i", egress_interface],
    ):
        check = ["iptables", "-C", "FORWARD", *rule, "-j", "ACCEPT"]
        code, _ = _run(check, check=False)
        if code != 0:
            _run(["iptables", "-A", "FORWARD", *rule, "-j", "ACCEPT"])


def create_tap(net_plan):
    """Create (or re-create) the tap device for a VM and bring it up."""
    delete_tap(net_plan["tap"])  # ignore leftovers from a crashed worker
    _run(["ip", "tuntap", "add", "dev", net_plan["tap"], "mode", "tap"])
    _run(["ip", "addr", "add", f"{net_plan['host_ip']}/30",
          "dev", net_plan["tap"]])
    _run(["ip", "link", "set", "dev", net_plan["tap"], "up"])


def validate_firewall(rules):
    """Normalize a small allow-list of TCP/UDP ingress rules."""
    if not isinstance(rules, list) or len(rules) > 32:
        raise NetworkError("firewall must contain at most 32 rules")
    normalized = []
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("protocol") not in ("tcp", "udp"):
            raise NetworkError("firewall protocol must be tcp or udp")
        try:
            port = int(rule.get("port"))
            source = str(ipaddress.IPv4Network(rule.get("source", "0.0.0.0/0"), strict=False))
        except (TypeError, ValueError):
            raise NetworkError("firewall source must be an IPv4 CIDR and port must be numeric")
        if not 1 <= port <= 65535:
            raise NetworkError("firewall port must be 1-65535")
        normalized.append({"protocol": rule["protocol"], "port": port, "source": source})
    return normalized


def firewall_chain(resource_id):
    return f"HCVM{int(resource_id)}"


def apply_firewall(resource_id, net_plan, rules):
    """Install a per-VM default-deny ingress chain before the guest is exposed."""
    rules = validate_firewall(rules or [])
    chain = firewall_chain(resource_id)
    _run(["iptables", "-N", chain], check=False)
    _run(["iptables", "-F", chain])
    _run(["iptables", "-A", chain, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"])
    for rule in rules:
        _run(["iptables", "-A", chain, "-s", rule["source"], "-p", rule["protocol"],
              "--dport", str(rule["port"]), "-j", "ACCEPT"])
    _run(["iptables", "-A", chain, "-j", "DROP"])
    hook = ["-d", net_plan["guest_ip"], "-o", net_plan["tap"], "-j", chain]
    code, _ = _run(["iptables", "-C", "FORWARD", *hook], check=False)
    if code != 0:
        _run(["iptables", "-I", "FORWARD", "1", *hook])
    return rules


def delete_firewall(resource_id, net_plan=None):
    chain = firewall_chain(resource_id)
    if net_plan:
        hook = ["-d", net_plan["guest_ip"], "-o", net_plan["tap"], "-j", chain]
        while _run(["iptables", "-D", "FORWARD", *hook], check=False)[0] == 0:
            pass
    _run(["iptables", "-F", chain], check=False)
    _run(["iptables", "-X", chain], check=False)


def delete_tap(tap):
    """Remove a tap device if it exists. Never raises."""
    _run(["ip", "link", "del", "dev", tap], check=False)


def tap_exists(tap):
    code, _ = _run(["ip", "link", "show", "dev", tap], check=False)
    return code == 0
