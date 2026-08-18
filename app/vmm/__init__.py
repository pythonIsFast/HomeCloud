"""Firecracker microVM management.

This package is the only place that touches the host: tap devices, NAT rules,
disk images and the firecracker processes themselves. It runs in the privileged
worker (``sudo python -m app.vmm``), never inside a web request.

Split of responsibility:

    net.py          tap device and NAT, the only part that needs root
    images.py       base image build and per-VM rootfs (works unprivileged)
    firecracker.py  config file, process spawn, Unix-socket API client
    worker.py       the job loop and process supervision

The web application talks to this package exclusively through the ``jobs``
table -- it never imports anything from here.
"""
