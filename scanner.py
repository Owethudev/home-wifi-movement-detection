"""
WiFi Network Scanner
====================
Discovers all active devices on your local network so you can
choose the best target(s) for motion detection.

Usage:
    python scanner.py
    python scanner.py --subnet 192.168.0

Requirements:
    pip install scapy rich
    (scapy needs root/admin for ARP scan — see note below)
"""

import subprocess
import platform
import ipaddress
import argparse
import socket
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich import box

console = Console()


@dataclass
class Device:
    ip:       str
    hostname: str
    rtt_ms:   Optional[float]
    stable:   bool   # True if RTT variance is low = good anchor for detection


def resolve_hostname(ip: str) -> str:
    try:
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return "—"


def ping_device(ip: str, count: int = 3) -> tuple[Optional[float], float]:
    """Returns (avg_rtt, variance) from `count` pings."""
    system = platform.system()
    rtts   = []
    for _ in range(count):
        try:
            if system == "Windows":
                cmd = ["ping", "-n", "1", "-w", "1000", ip]
            else:
                cmd = ["ping", "-c", "1", "-W", "1", ip]

            r = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
            out = r.stdout + r.stderr
            for line in out.splitlines():
                ll = line.lower()
                if "time=" in ll:
                    t = ll.split("time=")[1].split()[0]
                    rtts.append(float(t))
                    break
                if "average" in ll and "=" in line:
                    rtts.append(float(line.split("=")[1].strip().replace("ms", "")))
                    break
        except Exception:
            pass

    if not rtts:
        return None, 0.0

    avg = sum(rtts) / len(rtts)
    var = sum((r - avg) ** 2 for r in rtts) / len(rtts)
    return avg, var


def get_local_subnet() -> str:
    """Best-effort detection of local subnet prefix."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        parts = local_ip.split(".")
        return ".".join(parts[:3])
    except Exception:
        return "192.168.1"


def scan(subnet: str, max_workers: int = 50) -> list[Device]:
    devices = []
    ips     = [f"{subnet}.{i}" for i in range(1, 255)]

    console.print(f"[dim]Scanning {subnet}.0/24 …[/dim]")

    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(ping_device, ip): ip for ip in ips}
        for fut in as_completed(futures):
            ip = futures[fut]
            try:
                avg, var = fut.result()
                if avg is not None:
                    results[ip] = (avg, var)
            except Exception:
                pass

    console.print(f"[dim]Found {len(results)} active host(s). Resolving hostnames…[/dim]\n")

    for ip, (avg, var) in sorted(results.items(),
                                  key=lambda x: list(map(int, x[0].split(".")))):
        hostname = resolve_hostname(ip)
        stable   = var < 1.0  # low variance = reliable anchor
        devices.append(Device(ip=ip, hostname=hostname, rtt_ms=round(avg, 2), stable=stable))

    return devices


def print_table(devices: list[Device]):
    table = Table(
        title="Network devices",
        box=box.ROUNDED,
        show_lines=False,
        border_style="dim"
    )
    table.add_column("#",        style="dim",    width=4)
    table.add_column("IP",       style="cyan",   width=16)
    table.add_column("Hostname", width=30)
    table.add_column("RTT",      style="green",  width=10)
    table.add_column("Quality",  width=12)
    table.add_column("Recommended for motion?", width=24)

    for i, d in enumerate(devices, 1):
        quality = "[green]Stable[/green]" if d.stable else "[yellow]Variable[/yellow]"
        rec     = "[green]✓ Yes[/green]" if d.stable and d.rtt_ms and d.rtt_ms < 50 else \
                  "[yellow]Maybe[/yellow]" if d.stable else "[red]No[/red]"
        table.add_row(
            str(i),
            d.ip,
            d.hostname,
            f"{d.rtt_ms} ms" if d.rtt_ms else "—",
            quality,
            rec
        )

    console.print(table)
    console.print()
    console.print("[bold]Tip:[/bold] Use your router IP (usually lowest RTT) for best results.")
    console.print("[bold]Run:[/bold] python motion_detector.py --router [cyan]<IP>[/cyan]\n")


def main():
    parser = argparse.ArgumentParser(description="WiFi network device scanner")
    parser.add_argument("--subnet", default=None,
                        help="Subnet prefix e.g. 192.168.1 (auto-detected if omitted)")
    args = parser.parse_args()

    subnet  = args.subnet or get_local_subnet()
    devices = scan(subnet)

    if not devices:
        console.print("[red]No devices found. Check your network connection.[/red]")
        return

    print_table(devices)


if __name__ == "__main__":
    main()
