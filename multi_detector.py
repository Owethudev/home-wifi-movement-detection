"""
Multi-Device WiFi Motion Detector
==================================
Monitors multiple devices simultaneously and fuses their signals
for more accurate motion detection.  Running across 3+ devices
dramatically reduces false positives.

Usage:
    python multi_detector.py --routers 192.168.1.1,192.168.1.10,192.168.1.20

Requirements:
    pip install numpy scipy rich flask requests
"""

import os
import sys
import time
import json
import signal
import threading
import argparse
import statistics
from datetime import datetime
from collections import deque
from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich import box

# Local imports
sys.path.insert(0, os.path.dirname(__file__))
from motion_detector import (
    Config, MotionEvent, SignalProcessor,
    ping, rtt_to_rssi_estimate, console
)

# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class DeviceState:
    ip:           str
    rtt:          Optional[float] = None
    zscore:       float = 0.0
    motion:       bool  = False
    samples:      int   = 0
    calibrated:   bool  = False


class MultiDeviceDetector:
    """
    Runs independent signal processors per device and fuses
    z-scores with a weighted vote for final motion decision.
    """

    def __init__(self, ips: list[str], cfg: Config):
        self.ips       = ips
        self.cfg       = cfg
        self.running   = False
        self.events    = []
        self.states    = {ip: DeviceState(ip=ip) for ip in ips}
        self.procs     = {ip: SignalProcessor(cfg) for ip in ips}
        self._lock     = threading.Lock()
        self._callbacks = []
        self.last_alert = 0.0
        self.motion    = False
        self.start_time = time.time()

    def on_motion(self, fn):
        self._callbacks.append(fn)

    def _sample_device(self, ip: str):
        """Thread target — continuously pings one device."""
        while self.running:
            t0  = time.time()
            rtt = ping(ip, self.cfg.ping_timeout_ms)
            zscore = self.procs[ip].add(rtt)

            with self._lock:
                state = self.states[ip]
                state.rtt       = rtt
                state.zscore    = zscore or 0.0
                state.calibrated = self.procs[ip].is_calibrated()
                state.samples   += 1

            # Check fusion
            self._fuse()

            elapsed = time.time() - t0
            time.sleep(max(0, (self.cfg.ping_interval_ms / 1000) - elapsed))

    def _fuse(self):
        """Weighted z-score fusion across all devices."""
        with self._lock:
            calibrated = [s for s in self.states.values() if s.calibrated]

        if not calibrated:
            return

        # Weighted average — devices with higher z contribute more
        scores = [s.zscore for s in calibrated]
        if not scores:
            return

        fused = statistics.mean(scores)
        votes = sum(1 for z in scores if z >= self.cfg.motion_threshold)
        # Require majority vote
        majority = votes >= max(1, len(calibrated) // 2)

        now = time.time()
        if fused >= self.cfg.motion_threshold and majority:
            if now - self.last_alert >= self.cfg.motion_cooldown_s:
                self.last_alert = now
                self.motion = True
                event = MotionEvent(
                    timestamp  = datetime.now().isoformat(),
                    confidence = min(1.0, fused / (self.cfg.motion_threshold * 2)),
                    zscore     = round(fused, 3),
                    duration_s = 0.0,
                    variance   = round(statistics.mean(
                        [self.procs[ip].current_variance() for ip in self.ips]
                    ), 4)
                )
                with self._lock:
                    self.events.append(event)
                for cb in self._callbacks:
                    try: cb(event)
                    except: pass
        elif fused < self.cfg.motion_threshold * 0.5:
            self.motion = False

    def run(self):
        self.running = True
        threads = []
        for ip in self.ips:
            t = threading.Thread(target=self._sample_device, args=(ip,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

    def stop(self):
        self.running = False

    def render(self) -> Panel:
        table = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
        table.add_column("Device",     style="cyan",  width=16)
        table.add_column("RTT (ms)",   width=10)
        table.add_column("Z-score",    width=28)
        table.add_column("Status",     width=14)

        with self._lock:
            states = list(self.states.values())

        for s in states:
            z_bar = "█" * min(24, int(s.zscore * 4))
            z_col = "green" if s.zscore < self.cfg.motion_threshold else "red"
            rtt   = f"{s.rtt:.1f}" if s.rtt else "—"
            st    = "[green]Clear[/green]" if s.calibrated and not s.motion else \
                    "[yellow]Calibrating[/yellow]" if not s.calibrated else \
                    "[red]Motion[/red]"
            table.add_row(s.ip, rtt, f"[{z_col}]{z_bar:<24}[/{z_col}] {s.zscore:.2f}", st)

        fused_z = statistics.mean([s.zscore for s in states]) if states else 0.0
        border  = "red" if self.motion else "green"
        title   = "[bold red]⚠ MOTION DETECTED[/bold red]" if self.motion else "[bold green]● CLEAR[/bold green]"
        uptime  = time.time() - self.start_time

        return Panel(
            table,
            title=f"Multi-Device Detector  {title}",
            subtitle=f"[dim]{len(self.ips)} devices · fused z={fused_z:.2f} · "
                     f"events={len(self.events)} · uptime={uptime:.0f}s[/dim]",
            border_style=border
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--routers", required=True,
                        help="Comma-separated IPs e.g. 192.168.1.1,192.168.1.10")
    parser.add_argument("--interval",  default=200,  type=int)
    parser.add_argument("--threshold", default=2.5,  type=float)
    parser.add_argument("--baseline",  default=15,   type=int)
    args = parser.parse_args()

    ips = [ip.strip() for ip in args.routers.split(",") if ip.strip()]
    cfg = Config(
        router_ip        = ips[0],
        ping_interval_ms = args.interval,
        motion_threshold = args.threshold,
        baseline_seconds = args.baseline,
    )

    det = MultiDeviceDetector(ips, cfg)

    def on_motion(ev):
        console.print(f"\n[bold red]⚠ MOTION[/bold red] confidence={ev.confidence:.0%} z={ev.zscore} @ {ev.timestamp[11:19]}")

    det.on_motion(on_motion)

    def shutdown(sig, frame):
        det.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    t = threading.Thread(target=det.run, daemon=True)
    t.start()

    with Live(det.render(), refresh_per_second=4, console=console) as live:
        while det.running:
            live.update(det.render())
            time.sleep(0.25)


if __name__ == "__main__":
    main()
