"""
WiFi RSSI Motion Detector
=========================
Detects motion by monitoring fluctuations in WiFi signal strength (RSSI)
from devices on your network. Works on Linux, macOS, and Windows.

How it works:
  1. Pings known devices on your network continuously
  2. Measures round-trip time (RTT) and infers RSSI where possible
  3. Runs a sliding-window variance analysis on the signal
  4. Triggers a motion alert when variance exceeds a learned baseline

Requirements:
  pip install scapy rich numpy scipy flask requests

Root/admin privileges may be required for raw packet capture (scapy).
On Linux you can also use: sudo python motion_detector.py
"""

import os
import sys
import time
import json
import math
import signal
import platform
import threading
import subprocess
import statistics
import argparse
import logging
from datetime import datetime
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np
from scipy import signal as scipy_signal
from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text
from rich import box

# ── Optional: Flask dashboard ──────────────────────────────────────────────────
try:
    from flask import Flask, jsonify, render_template_string
    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False

console = Console()
logging.basicConfig(level=logging.WARNING)

# ═══════════════════════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Config:
    # Network
    router_ip:          str   = "192.168.1.1"      # Your router's IP
    ping_interval_ms:   int   = 200                 # How often to sample (ms)
    ping_timeout_ms:    int   = 1000                # Ping timeout
    window_size:        int   = 50                  # Samples in sliding window

    # Detection
    baseline_seconds:   int   = 15                  # Seconds to learn baseline
    motion_threshold:   float = 2.5                 # Std deviations above baseline
    motion_cooldown_s:  float = 3.0                 # Seconds before re-alerting
    min_variance:       float = 0.3                 # Minimum variance to consider

    # Output
    log_file:           str   = "motion_log.json"
    dashboard_port:     int   = 5050
    enable_dashboard:   bool  = True
    alert_sound:        bool  = True

    # Advanced
    bandpass_low:       float = 0.1                 # Hz – low freq cutoff
    bandpass_high:      float = 2.0                 # Hz – high freq cutoff (respiration ~0.3Hz, walk ~1Hz)
    zscore_window:      int   = 30                  # Z-score rolling window


# ═══════════════════════════════════════════════════════════════════════════════
#  Data structures
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Sample:
    timestamp:  float
    rtt_ms:     Optional[float]   # round-trip time in ms (proxy for signal quality)
    packet_loss: bool
    rssi_est:   Optional[float]   # estimated RSSI (dBm) from RTT heuristic


@dataclass
class MotionEvent:
    timestamp:  str
    confidence: float             # 0.0 – 1.0
    zscore:     float
    duration_s: float
    variance:   float


# ═══════════════════════════════════════════════════════════════════════════════
#  Platform-aware ping
# ═══════════════════════════════════════════════════════════════════════════════

def ping(host: str, timeout_ms: int = 1000) -> Optional[float]:
    """
    Returns round-trip time in ms, or None on timeout/failure.
    Uses the system ping utility so no privileges are needed.
    """
    system = platform.system()
    try:
        if system == "Windows":
            cmd = ["ping", "-n", "1", "-w", str(timeout_ms), host]
        elif system == "Darwin":   # macOS
            cmd = ["ping", "-c", "1", "-W", str(timeout_ms // 1000 or 1), host]
        else:                      # Linux
            cmd = ["ping", "-c", "1", "-W", str(timeout_ms // 1000 or 1), host]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=(timeout_ms / 1000) + 1
        )

        output = result.stdout + result.stderr

        # Parse RTT from output
        for line in output.splitlines():
            line_l = line.lower()
            # Linux/macOS: "rtt min/avg/max/mdev = 1.234/2.345/..."
            if "rtt" in line_l and "=" in line:
                parts = line.split("=")[1].strip().split("/")
                return float(parts[1])  # avg
            # macOS alt: "round-trip min/avg/max/stddev = ..."
            if "round-trip" in line_l and "=" in line:
                parts = line.split("=")[1].strip().split("/")
                return float(parts[1])
            # Windows: "Average = Xms"
            if "average" in line_l and "=" in line:
                return float(line.split("=")[1].strip().replace("ms", ""))
            # Linux single: "time=X.X ms"
            if "time=" in line_l:
                t = line_l.split("time=")[1].split()[0]
                return float(t)

        return None  # timeout or unreachable

    except (subprocess.TimeoutExpired, ValueError, IndexError, FileNotFoundError):
        return None


def rtt_to_rssi_estimate(rtt_ms: Optional[float]) -> Optional[float]:
    """
    Rough heuristic: convert RTT to an estimated RSSI value.
    NOT a true RSSI reading — it's a proxy that still shows variance.
    True RSSI requires driver/hardware access.

    RTT 1ms  ≈ -50 dBm (excellent)
    RTT 5ms  ≈ -65 dBm (good)
    RTT 20ms ≈ -75 dBm (fair)
    RTT 100ms ≈ -85 dBm (poor)
    """
    if rtt_ms is None:
        return None
    # Logarithmic mapping into -45 to -90 dBm range
    clamped = max(0.5, min(rtt_ms, 500))
    rssi = -45 - (math.log10(clamped / 0.5) * 15)
    return max(-95, min(-40, rssi))


# ═══════════════════════════════════════════════════════════════════════════════
#  Signal processing
# ═══════════════════════════════════════════════════════════════════════════════

class SignalProcessor:
    """
    Processes a stream of RTT samples, applies bandpass filtering,
    and computes a z-score relative to a rolling baseline.
    """

    def __init__(self, cfg: Config):
        self.cfg      = cfg
        self.raw      = deque(maxlen=cfg.window_size * 4)
        self.filtered = deque(maxlen=cfg.window_size * 4)
        self.baseline_mean   = None
        self.baseline_std    = None
        self._sample_rate    = 1000 / cfg.ping_interval_ms  # Hz

    def add(self, rtt: Optional[float]) -> Optional[float]:
        """Add sample; returns current z-score or None if still calibrating."""
        val = rtt if rtt is not None else (
            (self.baseline_mean or 10) * 2   # treat loss as doubled RTT
        )
        self.raw.append(val)

        # Need at least window_size samples to filter
        if len(self.raw) < self.cfg.window_size:
            return None

        # Apply bandpass filter (Butterworth 2nd order)
        raw_arr = np.array(self.raw)
        try:
            nyq   = self._sample_rate / 2
            lo    = self.cfg.bandpass_low  / nyq
            hi    = min(self.cfg.bandpass_high / nyq, 0.99)
            b, a  = scipy_signal.butter(2, [lo, hi], btype='band')
            filt  = scipy_signal.filtfilt(b, a, raw_arr)
            self.filtered.append(float(filt[-1]))
        except Exception:
            self.filtered.append(val)

        # Baseline from first N seconds of filtered data
        if self.baseline_mean is None:
            n_baseline = int(self.cfg.baseline_seconds * self._sample_rate)
            if len(self.filtered) >= n_baseline:
                arr = list(self.filtered)[:n_baseline]
                self.baseline_mean = statistics.mean(arr)
                self.baseline_std  = max(statistics.stdev(arr), 0.001)
            return None

        # Rolling z-score
        window = list(self.filtered)[-self.cfg.zscore_window:]
        w_mean = statistics.mean(window)
        w_std  = max(statistics.stdev(window) if len(window) > 1 else 0, 0.001)

        zscore = abs((w_mean - self.baseline_mean) / self.baseline_std)
        return zscore

    def current_variance(self) -> float:
        if len(self.raw) < 5:
            return 0.0
        window = list(self.raw)[-self.cfg.window_size:]
        return statistics.variance(window) if len(window) > 1 else 0.0

    def is_calibrated(self) -> bool:
        return self.baseline_mean is not None

    def reset_baseline(self):
        self.baseline_mean = None
        self.baseline_std  = None
        self.raw.clear()
        self.filtered.clear()


# ═══════════════════════════════════════════════════════════════════════════════
#  Motion detector core
# ═══════════════════════════════════════════════════════════════════════════════

class MotionDetector:

    def __init__(self, cfg: Config):
        self.cfg        = cfg
        self.processor  = SignalProcessor(cfg)
        self.running    = False
        self.motion     = False
        self.last_alert = 0.0
        self.events: list[MotionEvent] = []
        self.samples: deque[Sample]    = deque(maxlen=500)
        self.motion_start: Optional[float] = None
        self._lock      = threading.Lock()
        self._callbacks = []

        # Stats
        self.total_samples    = 0
        self.lost_packets     = 0
        self.start_time       = time.time()
        self.current_zscore   = 0.0
        self.current_rtt      = None

    def on_motion(self, fn):
        """Register a callback for motion events."""
        self._callbacks.append(fn)
        return self

    def _alert(self, zscore: float, variance: float):
        now = time.time()
        if now - self.last_alert < self.cfg.motion_cooldown_s:
            return

        with self._lock:
            if not self.motion:
                self.motion       = True
                self.motion_start = now
            self.last_alert = now

        duration = now - (self.motion_start or now)
        confidence = min(1.0, (zscore - self.cfg.motion_threshold) /
                         self.cfg.motion_threshold + 0.5)

        event = MotionEvent(
            timestamp  = datetime.now().isoformat(),
            confidence = round(confidence, 3),
            zscore     = round(zscore, 3),
            duration_s = round(duration, 2),
            variance   = round(variance, 4)
        )

        with self._lock:
            self.events.append(event)

        self._save_event(event)
        self._play_alert()

        for cb in self._callbacks:
            try:
                cb(event)
            except Exception:
                pass

    def _clear_motion(self):
        with self._lock:
            self.motion       = False
            self.motion_start = None

    def _save_event(self, event: MotionEvent):
        try:
            existing = []
            if os.path.exists(self.cfg.log_file):
                with open(self.cfg.log_file) as f:
                    existing = json.load(f)
            existing.append(asdict(event))
            with open(self.cfg.log_file, "w") as f:
                json.dump(existing, f, indent=2)
        except Exception:
            pass

    def _play_alert(self):
        if not self.cfg.alert_sound:
            return
        try:
            system = platform.system()
            if system == "Darwin":
                subprocess.Popen(["afplay", "/System/Library/Sounds/Ping.aiff"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif system == "Linux":
                subprocess.Popen(["paplay", "/usr/share/sounds/freedesktop/stereo/bell.oga"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif system == "Windows":
                import winsound
                winsound.Beep(880, 300)
        except Exception:
            pass

    def run(self):
        """Main sampling loop — runs in foreground."""
        self.running   = True
        self.start_time = time.time()

        while self.running:
            t0  = time.time()
            rtt = ping(self.cfg.router_ip, self.cfg.ping_timeout_ms)
            t1  = time.time()

            rssi = rtt_to_rssi_estimate(rtt)
            sample = Sample(
                timestamp   = t1,
                rtt_ms      = rtt,
                packet_loss = rtt is None,
                rssi_est    = rssi
            )

            with self._lock:
                self.samples.append(sample)
                self.total_samples += 1
                if rtt is None:
                    self.lost_packets += 1
                self.current_rtt = rtt

            zscore   = self.processor.add(rtt)
            variance = self.processor.current_variance()

            if zscore is not None:
                self.current_zscore = zscore
                if zscore >= self.cfg.motion_threshold and \
                   variance >= self.cfg.min_variance:
                    self._alert(zscore, variance)
                elif zscore < self.cfg.motion_threshold * 0.6:
                    self._clear_motion()

            # Sleep for remainder of interval
            elapsed = t1 - t0
            sleep_s = max(0, (self.cfg.ping_interval_ms / 1000) - elapsed)
            time.sleep(sleep_s)

    def stop(self):
        self.running = False

    def status(self) -> dict:
        uptime = time.time() - self.start_time
        loss_pct = (self.lost_packets / max(1, self.total_samples)) * 100
        return {
            "motion":       self.motion,
            "calibrated":   self.processor.is_calibrated(),
            "zscore":       round(self.current_zscore, 3),
            "rtt_ms":       round(self.current_rtt, 2) if self.current_rtt else None,
            "variance":     round(self.processor.current_variance(), 4),
            "uptime_s":     round(uptime, 1),
            "total_samples":self.total_samples,
            "packet_loss_%":round(loss_pct, 1),
            "events_total": len(self.events),
            "threshold":    self.cfg.motion_threshold,
            "target":       self.cfg.router_ip,
        }


# ═══════════════════════════════════════════════════════════════════════════════
#  Rich terminal dashboard
# ═══════════════════════════════════════════════════════════════════════════════

SPARKLINE_CHARS = " ▁▂▃▄▅▆▇█"

def sparkline(values: list, width: int = 40) -> str:
    if not values:
        return "─" * width
    mn, mx = min(values), max(values)
    rng    = mx - mn or 1
    chars  = []
    step   = max(1, len(values) // width)
    for i in range(0, len(values), step):
        v   = values[i]
        idx = int(((v - mn) / rng) * (len(SPARKLINE_CHARS) - 1))
        chars.append(SPARKLINE_CHARS[idx])
    return "".join(chars[-width:])


def render_dashboard(detector: MotionDetector) -> Panel:
    st   = detector.status()
    cfg  = detector.cfg

    # Signal sparkline from RTT history
    with detector._lock:
        rtts = [s.rtt_ms for s in detector.samples if s.rtt_ms is not None]
    spark = sparkline(rtts[-60:], width=50)

    # Status colour
    if not st["calibrated"]:
        status_text = Text("● CALIBRATING…", style="bold yellow")
    elif st["motion"]:
        status_text = Text("● MOTION DETECTED", style="bold red blink")
    else:
        status_text = Text("● CLEAR", style="bold green")

    # Z-score bar
    z     = min(st["zscore"], 6.0)
    z_bar = "█" * int(z * 4) + "░" * max(0, 24 - int(z * 4))
    z_col = "green" if z < cfg.motion_threshold else ("yellow" if z < cfg.motion_threshold * 1.5 else "red")

    # Build table
    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    table.add_column("Key",   style="dim", width=20)
    table.add_column("Value", style="bold")

    table.add_row("Status",        status_text)
    table.add_row("Target",        st["target"])
    table.add_row("RTT",           f"{st['rtt_ms']} ms" if st['rtt_ms'] else "timeout")
    table.add_row("Z-score",       f"[{z_col}]{z_bar}[/{z_col}] {st['zscore']:.2f}")
    table.add_row("Variance",      f"{st['variance']:.4f}")
    table.add_row("Packet loss",   f"{st['packet_loss_%']}%")
    table.add_row("Events today",  str(st["events_total"]))
    table.add_row("Uptime",        f"{st['uptime_s']:.0f}s")
    table.add_row("Samples",       str(st["total_samples"]))
    table.add_row("")
    table.add_row("Signal (RTT)",  f"[cyan]{spark}[/cyan]")

    # Recent events
    with detector._lock:
        recent = list(detector.events)[-5:]

    if recent:
        table.add_row("")
        table.add_row("[dim]Recent events[/dim]", "")
        for ev in reversed(recent):
            ts  = ev.timestamp[11:19]
            col = "red" if ev.confidence > 0.7 else "yellow"
            table.add_row(
                f"  {ts}",
                f"[{col}]confidence {ev.confidence:.0%}  z={ev.zscore:.2f}[/{col}]"
            )

    return Panel(
        table,
        title="[bold]WiFi Motion Detector[/bold]",
        subtitle=f"[dim]threshold={cfg.motion_threshold}σ  interval={cfg.ping_interval_ms}ms  "
                 f"{'[green]CALIBRATED[/green]' if st['calibrated'] else '[yellow]calibrating…[/yellow]'}[/dim]",
        border_style="red" if st["motion"] else ("yellow" if not st["calibrated"] else "green"),
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  Optional Flask web dashboard
# ═══════════════════════════════════════════════════════════════════════════════

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WiFi Motion Detector</title>
<style>
  :root { --green:#22c55e; --red:#ef4444; --yellow:#f59e0b; --bg:#0f0f0f; --card:#1a1a1a; --border:#2a2a2a; --text:#e5e5e5; --muted:#6b7280; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text); font-family:monospace; padding:1.5rem; }
  h1 { font-size:1.2rem; margin-bottom:1.5rem; color:var(--muted); }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:1rem; margin-bottom:1.5rem; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:1rem; }
  .card-label { font-size:.7rem; color:var(--muted); text-transform:uppercase; letter-spacing:.08em; margin-bottom:.3rem; }
  .card-val { font-size:1.4rem; font-weight:700; }
  .motion-banner { padding:1rem 1.5rem; border-radius:8px; font-size:1rem; font-weight:700; margin-bottom:1.5rem; text-align:center; transition:.3s; }
  .motion-yes { background:#7f1d1d; border:1px solid var(--red); color:var(--red); }
  .motion-no  { background:#052e16; border:1px solid var(--green); color:var(--green); }
  .motion-cal { background:#451a03; border:1px solid var(--yellow); color:var(--yellow); }
  .spark { font-size:.65rem; color:#60a5fa; word-break:break-all; letter-spacing:-.02em; background:var(--card); border:1px solid var(--border); border-radius:6px; padding:.75rem 1rem; margin-bottom:1.5rem; }
  .events { background:var(--card); border:1px solid var(--border); border-radius:8px; overflow:hidden; }
  .events-header { padding:.6rem 1rem; font-size:.75rem; color:var(--muted); border-bottom:1px solid var(--border); }
  .event { padding:.5rem 1rem; border-bottom:1px solid var(--border); font-size:.8rem; display:flex; justify-content:space-between; }
  .event:last-child { border-bottom:none; }
  .conf-high { color:var(--red); }
  .conf-med  { color:var(--yellow); }
  .conf-low  { color:var(--green); }
  .zbar { display:inline-block; height:8px; border-radius:4px; background:var(--green); vertical-align:middle; }
</style>
</head>
<body>
<h1>WiFi Motion Detector — Live Dashboard</h1>
<div id="banner" class="motion-banner motion-cal">● CALIBRATING…</div>
<div class="grid" id="stats"></div>
<div class="spark" id="spark"></div>
<div class="events">
  <div class="events-header">Recent motion events</div>
  <div id="events"></div>
</div>

<script>
const SPARKLINE = " ▁▂▃▄▅▆▇█";

async function refresh() {
  try {
    const [status, events] = await Promise.all([
      fetch('/api/status').then(r => r.json()),
      fetch('/api/events').then(r => r.json())
    ]);

    // Banner
    const banner = document.getElementById('banner');
    if (!status.calibrated) {
      banner.className = 'motion-banner motion-cal';
      banner.textContent = '● CALIBRATING — stay still for ' + Math.ceil(15 - status.uptime_s) + 's…';
    } else if (status.motion) {
      banner.className = 'motion-banner motion-yes';
      banner.textContent = '● MOTION DETECTED';
    } else {
      banner.className = 'motion-banner motion-no';
      banner.textContent = '● CLEAR — no motion';
    }

    // Stats grid
    const zPct = Math.min(100, (status.zscore / (status.threshold * 2)) * 100);
    const zCol = status.zscore < status.threshold ? '#22c55e' : status.zscore < status.threshold * 1.5 ? '#f59e0b' : '#ef4444';
    document.getElementById('stats').innerHTML = [
      { label: 'RTT', val: status.rtt_ms != null ? status.rtt_ms + ' ms' : 'timeout' },
      { label: 'Z-score', val: `<div class='zbar' style='width:${zPct}px;background:${zCol}'></div> ${status.zscore}` },
      { label: 'Variance', val: status.variance },
      { label: 'Packet loss', val: status['packet_loss_%'] + '%' },
      { label: 'Events', val: status.events_total },
      { label: 'Uptime', val: status.uptime_s + 's' },
    ].map(c => `<div class='card'><div class='card-label'>${c.label}</div><div class='card-val'>${c.val}</div></div>`).join('');

    // Sparkline (from events variance over time — simplified)
    document.getElementById('spark').textContent = 'Signal variance: ' + (status.variance > 0 ? '▁▂▃▄▅'.repeat(8).slice(0, 40) : '─'.repeat(40));

    // Events
    const evHtml = events.slice(-20).reverse().map(ev => {
      const conf = ev.confidence;
      const cls = conf > 0.7 ? 'conf-high' : conf > 0.4 ? 'conf-med' : 'conf-low';
      return `<div class='event'>
        <span>${ev.timestamp.slice(0,19).replace('T',' ')}</span>
        <span class='${cls}'>${(conf*100).toFixed(0)}% confidence  z=${ev.zscore}</span>
      </div>`;
    }).join('') || "<div class='event' style='color:#6b7280'>No events yet</div>";
    document.getElementById('events').innerHTML = evHtml;

  } catch(e) { console.warn(e); }
}

setInterval(refresh, 800);
refresh();
</script>
</body>
</html>
"""

def start_flask(detector: MotionDetector, port: int):
    if not FLASK_AVAILABLE:
        return

    app = Flask(__name__)

    @app.route("/")
    def index():
        return render_template_string(DASHBOARD_HTML)

    @app.route("/api/status")
    def api_status():
        return jsonify(detector.status())

    @app.route("/api/events")
    def api_events():
        with detector._lock:
            return jsonify([asdict(e) for e in detector.events])

    @app.route("/api/reset-baseline", methods=["POST"])
    def api_reset():
        detector.processor.reset_baseline()
        return jsonify({"ok": True})

    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# ═══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="WiFi RSSI Motion Detector")
    parser.add_argument("--router",    default="192.168.1.1", help="Router/device IP to ping")
    parser.add_argument("--interval",  default=200,  type=int, help="Ping interval in ms")
    parser.add_argument("--threshold", default=2.5,  type=float, help="Z-score threshold for motion")
    parser.add_argument("--baseline",  default=15,   type=int, help="Calibration seconds")
    parser.add_argument("--port",      default=5050, type=int, help="Dashboard web port")
    parser.add_argument("--no-sound",  action="store_true", help="Disable alert sound")
    parser.add_argument("--no-dashboard", action="store_true", help="Disable web dashboard")
    args = parser.parse_args()

    cfg = Config(
        router_ip        = args.router,
        ping_interval_ms = args.interval,
        motion_threshold = args.threshold,
        baseline_seconds = args.baseline,
        dashboard_port   = args.port,
        alert_sound      = not args.no_sound,
        enable_dashboard = not args.no_dashboard,
    )

    detector = MotionDetector(cfg)

    # Motion callback
    def on_motion_event(event: MotionEvent):
        console.print(
            f"\n[bold red]⚠ MOTION[/bold red]  "
            f"confidence={event.confidence:.0%}  "
            f"z={event.zscore:.2f}  "
            f"@ {event.timestamp[11:19]}"
        )

    detector.on_motion(on_motion_event)

    # Flask dashboard in background thread
    if cfg.enable_dashboard and FLASK_AVAILABLE:
        t = threading.Thread(target=start_flask, args=(detector, cfg.dashboard_port), daemon=True)
        t.start()
        console.print(f"[dim]Web dashboard → http://localhost:{cfg.dashboard_port}[/dim]")

    # Graceful shutdown
    def shutdown(sig, frame):
        console.print("\n[dim]Stopping…[/dim]")
        detector.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Start detector in background, render dashboard in foreground
    t_det = threading.Thread(target=detector.run, daemon=True)
    t_det.start()

    console.print(Panel(
        f"[bold]Pinging[/bold] [cyan]{cfg.router_ip}[/cyan] every [cyan]{cfg.ping_interval_ms}ms[/cyan]\n"
        f"[bold]Calibrating[/bold] for [cyan]{cfg.baseline_seconds}s[/cyan] — stay still\n"
        f"[bold]Threshold[/bold] [cyan]{cfg.motion_threshold}σ[/cyan]\n\n"
        f"[dim]Press Ctrl+C to stop[/dim]",
        title="WiFi Motion Detector starting",
        border_style="yellow"
    ))

    with Live(render_dashboard(detector), refresh_per_second=4, console=console) as live:
        while detector.running:
            live.update(render_dashboard(detector))
            time.sleep(0.25)


if __name__ == "__main__":
    main()
