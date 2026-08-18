"""The small, local bridge between the web UI and a VM serial console.

The privileged worker owns one Unix socket and FIFO per running VM. The web
process can only send bytes through that socket; it never opens a VM's files,
touches KVM, or starts a process. Socket permissions follow the VM directory's
group, which is shared with the unprivileged web service in the deployment.
"""

import os
import socket
import threading

MAX_INPUT_BYTES = 4096


class ConsoleError(Exception):
    pass


def input_socket_path(vm_dir):
    return os.path.join(vm_dir, "console-input.sock")


def input_fifo_path(vm_dir):
    return os.path.join(vm_dir, "console-input.fifo")


def send_input(vm_dir, data):
    """Send validated terminal bytes to the worker for one VM."""
    if not data or len(data) > MAX_INPUT_BYTES:
        raise ConsoleError("terminal input must contain 1-4096 bytes")

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(1.5)
    try:
        client.connect(input_socket_path(vm_dir))
        client.sendall(data)
        if client.recv(2) != b"OK":
            raise ConsoleError("terminal is not available")
    except (OSError, TimeoutError) as error:
        raise ConsoleError(
            "terminal is not available; start or restart the instance"
        ) from error
    finally:
        client.close()


class ConsoleBridge:
    """Serve terminal input for one running Firecracker process."""

    def __init__(self, vm_dir):
        self.vm_dir = vm_dir
        self.socket_path = input_socket_path(vm_dir)
        self.fifo_path = input_fifo_path(vm_dir)
        self.listener = None
        self.thread = None
        self.stopping = threading.Event()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.socket_path)
        os.chown(self.socket_path, -1, os.stat(self.vm_dir).st_gid)
        os.chmod(self.socket_path, 0o660)
        self.listener.listen(8)
        self.listener.settimeout(0.5)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        if self.listener is not None:
            self.listener.close()
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

    def _serve(self):
        while not self.stopping.is_set():
            try:
                connection, _ = self.listener.accept()
            except (OSError, TimeoutError):
                continue
            with connection:
                try:
                    data = connection.recv(MAX_INPUT_BYTES + 1)
                    if not data or len(data) > MAX_INPUT_BYTES:
                        continue
                    fd = os.open(self.fifo_path, os.O_WRONLY | os.O_NONBLOCK)
                    try:
                        os.write(fd, data)
                    finally:
                        os.close(fd)
                    connection.sendall(b"OK")
                except OSError:
                    # The guest may have exited between the UI request and the
                    # write. The client gets a short, generic unavailable error.
                    pass

