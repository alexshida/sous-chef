"""Working out which address to serve on.

Binding to 0.0.0.0 is the usual shortcut for "let my phone reach it", but it
also answers on whatever network the machine happens to be attached to — a
café, a hotel, a conference. This module finds the Tailscale address instead,
so the app is reachable from your own devices anywhere in the world — your
phone in the grocery aisle included — and from nothing else, regardless of
which wifi the laptop is on.

Shared with Trainer, which solved this first.

Tailscale hands out addresses in the CGNAT range 100.64.0.0/10, which is how
its interface is recognised without shelling out when the CLI is absent.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import subprocess

TAILSCALE_RANGE = ipaddress.ip_network("100.64.0.0/10")


class NoTailnet(RuntimeError):
    """Tailscale is not installed, not running, or not logged in."""


# Where the CLI lives when it is not on PATH — which is the normal case under
# launchd, whose PATH is just /usr/bin:/bin:/usr/sbin:/sbin.
#
# Order matters. /Applications/Tailscale.app/Contents/MacOS/Tailscale is the GUI
# app binary, not the CLI: asked for an IP it tries to start the GUI, prints
# "The Tailscale GUI failed to start" — and exits 0 while doing it. The real CLI
# that the Mac app installs at /usr/local/bin/tailscale has to be tried first.
_CLI_PATHS = (
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
)


def _cli() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    return next((p for p in _CLI_PATHS if os.path.exists(p)), None)


def tailscale_ip() -> str:
    """This machine's Tailscale IPv4 address, as reported by Tailscale itself.

    Deliberately refuses to infer this from network interfaces. 100.64.0.0/10 is
    the shared CGNAT range — carriers, corporate VPNs and other tunnels all use
    it — so an address in that range is no evidence Tailscale is involved. This
    machine has a utun interface on 100.64.x.x belonging to an unrelated VPN;
    binding to it while reporting "your tailnet only" would hand you a
    private-looking URL served on somebody else's network.

    If Tailscale cannot confirm the address, this raises rather than guesses.
    """
    cli = _cli()
    if not cli:
        raise NoTailnet(
            "Tailscale is not installed. See the Remote access section of README.md — "
            "`brew install --cask tailscale`, then sign in."
        )
    try:
        out = subprocess.run([cli, "ip", "-4"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        raise NoTailnet(f"Could not ask Tailscale for its address: {e}")

    # The exit code is not enough to go on: the GUI binary reports failure on
    # stdout and still exits 0. Trust only a well-formed address.
    for line in out.stdout.splitlines():
        line = line.strip()
        if _is_tailscale(line):
            return line

    detail = (out.stderr or out.stdout).strip().splitlines()
    hint = detail[0] if detail else "it returned no address"
    raise NoTailnet(f"Tailscale ({cli}) gave no usable address — {hint}")


def _is_tailscale(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr.strip()) in TAILSCALE_RANGE
    except ValueError:
        return False


def _local_addresses() -> list[str]:
    addrs: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.append(info[4][0])
    except OSError:
        pass
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("inet "):
                addrs.append(line.split()[1])
    except (OSError, subprocess.SubprocessError):
        pass
    return addrs


def lan_ip() -> str | None:
    """Best-guess LAN address, for printing a same-wifi URL."""
    for addr in _local_addresses():
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_private and not ip.is_loopback and not _is_tailscale(addr):
            return addr  # _is_tailscale excludes CGNAT, which is never a LAN address
    return None


def resolve_host(host: str) -> tuple[str, str]:
    """Turn a --host value into (bind_address, label).

    "tailscale" binds to the tailnet address only. Anything else is passed
    through untouched.
    """
    if host != "tailscale":
        return host, host
    addr = tailscale_ip()
    return addr, addr


def is_allowed_client(addr: str | None) -> bool:
    """Whether a request from this address may be served.

    Loopback and the tailnet only. This is belt-and-braces behind the choice of
    bind address: if the app is ever started on a wider interface, a café or
    hotel network still cannot read your calendar or run the recipe chef on
    your Claude account.
    """
    if not addr:
        return False
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.is_loopback or ip in TAILSCALE_RANGE


def bind_sockets(port: int, tailnet: bool = True) -> tuple[list, list[str], str | None]:
    """Open listening sockets, returning (sockets, urls, tailnet_problem).

    Loopback is always bound and always first: it is the one address that
    survives changing networks, so the laptop itself keeps working whether it is
    at home, on a train, or on hotel wifi. The tailnet is added when available
    and skipped — never fatal — when it is not, so a missing or sleeping
    Tailscale can't take the app down.
    """
    sockets, urls = [], []
    problem: str | None = None

    loopback = _listen("127.0.0.1", port)
    sockets.append(loopback)
    urls.append(f"http://localhost:{port}")

    if tailnet:
        try:
            addr = tailscale_ip()
        except NoTailnet as e:
            # Reported, not swallowed — a silent skip here left the service
            # bound to loopback with no way to tell why.
            problem = str(e)
        else:
            try:
                sockets.append(_listen(addr, port))
                urls.append(f"http://{addr}:{port}")
            except OSError as e:
                problem = f"Tailscale address {addr} could not be bound — {e}"

    return sockets, urls, problem


def _listen(addr: str, port: int):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((addr, port))
    sock.listen(128)
    sock.set_inheritable(True)
    return sock
