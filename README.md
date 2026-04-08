# WiFi Motion Detector

Detects motion in your home by monitoring fluctuations in WiFi signal
strength (RTT / RSSI proxies) from devices on your local network.
No cameras, no special hardware — just Python and your existing router.

---

## How it actually works

Human movement disrupts WiFi radio waves as they bounce around a room.
Even small movements (raising an arm, walking past) cause measurable
changes in the round-trip time of packets between your computer and
your router. This system:

1. Pings your router (or any network device) at 200ms intervals
2. Applies a Butterworth bandpass filter (0.1–2.0 Hz) to isolate
   motion-frequency components from general network noise
3. Computes a rolling z-score vs a calibrated baseline
4. Triggers a motion alert when the z-score exceeds a threshold

The frequency band 0.1–2.0 Hz is chosen because:
- Human walking: ~0.8–1.2 Hz
- Respiration (breathing): ~0.2–0.4 Hz
- Limb movement: ~0.5–1.5 Hz

---

## Quick start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Find your router IP

**macOS/Linux:**
```bash
ip route | grep default   # Linux
netstat -nr | grep default  # macOS
```

**Windows:**
```cmd
ipconfig
# Look for "Default Gateway"
```
Typically: `192.168.1.1` or `192.168.0.1`

### 3. Scan your network (optional but recommended)

```bash
python scanner.py
```
This shows all active devices and recommends the best target(s).

### 4. Run the detector

```bash
# Single device (your router)
python motion_detector.py --router 192.168.1.1

# Custom threshold (lower = more sensitive)
python motion_detector.py --router 192.168.1.1 --threshold 2.0

# Longer calibration period
python motion_detector.py --router 192.168.1.1 --baseline 30

# Multiple devices (more accurate, fewer false positives)
python multi_detector.py --routers 192.168.1.1,192.168.1.10,192.168.1.20
```

### 5. Open the web dashboard

After starting, open your browser to:
```
http://localhost:5050
```

---

## Command-line options

| Flag | Default | Description |
|------|---------|-------------|
| `--router` | `192.168.1.1` | IP to ping for signal monitoring |
| `--interval` | `200` | Ping interval in milliseconds |
| `--threshold` | `2.5` | Z-score threshold for motion alert |
| `--baseline` | `15` | Calibration seconds (stay still) |
| `--port` | `5050` | Web dashboard port |
| `--no-sound` | off | Disable audio alert |
| `--no-dashboard` | off | Disable web dashboard |

---

## Tips for best results

### Placement
- Run on a laptop/desktop in the room you want to monitor
- Place your device so WiFi signal passes through the monitored area
- The system works best in a single room — walls reduce accuracy

### Calibration
- When starting, stay completely still for the calibration period
- The system learns what "no motion" looks like for your environment
- Re-calibrate if you move to a different location (Ctrl+C, restart)

### Reducing false positives
- Use `--threshold 3.0` for less sensitivity
- Use multiple devices (`multi_detector.py`) — requires majority vote
- Avoid running during high network activity (large downloads/uploads)

### Improving sensitivity
- Use `--threshold 2.0` for more sensitivity
- Use `--interval 100` for faster sampling (more CPU)
- Place the monitoring device closer to the area of interest

---

## Limitations (be honest with yourself)

| What it CAN detect | What it CANNOT do |
|-------------------|-------------------|
| A person walking through a room | Identify WHO is moving |
| Someone standing up / sitting down | Count people |
| Repeated motion patterns | Detect motion in adjacent rooms reliably |
| General presence/absence | Work through thick concrete walls |

This is NOT a replacement for a proper security camera or PIR sensor.
It is a supplementary, privacy-preserving layer that works without
any visual data.

---

## Upgrading to true CSI sensing

If you want true Channel State Information (raw PHY-layer data):

1. **Hardware required:** Router with Atheros AR9300+ chipset
   OR a second WiFi card with `nexmon` CSI patch (Broadcom BCM43455)
2. **Linux only**
3. Tools: `Atheros-CSI-Tool`, `nexmon_csi`, or ESP32 with custom firmware
4. Python libraries: `csiread` (pip install csiread)

CSI gives you ~30 subcarrier measurements vs our single RTT proxy —
dramatically more accurate, enables breathing detection at 5m range.

---

## File structure

```
wifi_motion_detector/
├── motion_detector.py   # Main single-device detector
├── multi_detector.py    # Multi-device fusion detector
├── scanner.py           # Network device scanner
├── requirements.txt     # Python dependencies
├── README.md            # This file
└── motion_log.json      # Auto-created: event log
```

---

## Troubleshooting

**"Permission denied" on Linux:**
```bash
sudo python motion_detector.py --router 192.168.1.1
# or give ping CAP_NET_RAW:
sudo setcap cap_net_raw+ep /bin/ping
```

**Always showing motion / never calibrating:**
- Your network may be too noisy — increase `--threshold`
- Check for background downloads/streaming during calibration

**RTT always showing "timeout":**
- Verify the IP is correct: `ping 192.168.1.1` in terminal
- Your router may block ICMP — try another device's IP instead

**No sound alerts:**
- macOS: requires `afplay` (built-in)
- Linux: requires `pulseaudio` (`sudo apt install pulseaudio`)
- Windows: built-in via `winsound`
