# WiFi Motion Detector (Node.js)

Detects motion in your home by monitoring fluctuations in WiFi ping round-trip times (RTT) from your router.

## How it works

1. Pings your router (or a specific device) continuously
2. Maintains a sliding window of RTT values
3. Calculates variance in the window
4. Compares to baseline variance established during calibration
5. Triggers motion alert when variance exceeds threshold

## Quick start

1. Install dependencies:
   ```bash
   npm install
   ```

2. Find your router IP (usually 192.168.1.1 or 192.168.0.1) or scan for devices:
   ```bash
   node scanner.js
   ```
   This will list reachable devices on your network.

3. To monitor a specific room, set the target to a device in that room:
   ```bash
   TARGET_IP=192.168.0.100 npm start
   ```
   Replace 192.168.0.100 with the IP of a device in your room (e.g., your computer or a smart device).

4. Open your browser and go to `http://localhost:3000` to see the web interface.

## Requirements

- Node.js
- Network access to your router

## Web Interface

The simplistic UI shows:
- Status indicator (Calibrating/Ready/Motion Detected)
- Real-time log of detections with timestamps
- **Scan Network** button to discover devices on your network

## Differences from previous version

- Uses Node.js instead of Python
- Simpler variance-based detection instead of complex filtering
- Web-based UI for monitoring detections
