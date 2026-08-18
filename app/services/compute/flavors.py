"""Instance size catalogue.

A fixed catalogue instead of free-form vCPU/RAM input, for the same reason
every cloud does it: quota arithmetic, capacity planning and the UI all get
simpler when there are eight possibilities rather than a continuum.

Sizes are deliberately small -- a homelab host has a few GB to spare, not a few
hundred. Add entries here; nothing else needs to change.
"""

FLAVORS = {
    "hc.nano":   {"vcpu": 1, "memory_mb": 256,  "disk_gb": 1},
    "hc.micro":  {"vcpu": 1, "memory_mb": 512,  "disk_gb": 2},
    "hc.small":  {"vcpu": 1, "memory_mb": 1024, "disk_gb": 5},
    "hc.medium": {"vcpu": 2, "memory_mb": 2048, "disk_gb": 10},
    "hc.large":  {"vcpu": 4, "memory_mb": 4096, "disk_gb": 20},
}

DEFAULT_FLAVOR = "hc.micro"


def get(name):
    """Return a flavor dict, or None if the name is unknown."""
    flavor = FLAVORS.get(name)
    if flavor is None:
        return None
    return dict(flavor, name=name)


def catalogue():
    """All flavors as a list, smallest first -- for the API and the UI select."""
    return [
        dict(spec, name=name)
        for name, spec in sorted(
            FLAVORS.items(), key=lambda item: (item[1]["memory_mb"], item[1]["vcpu"])
        )
    ]
