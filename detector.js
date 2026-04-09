const { exec } = require('child_process');
const math = require('mathjs');
const express = require('express');
const http = require('http');
const socketIo = require('socket.io');
const NetworkScanner = require('./scanner');

class MotionDetector {
  constructor(routerIP = process.env.TARGET_IP || '192.168.0.180', interval = 200) {
    this.routerIP = routerIP;
    this.interval = interval;
    this.rtts = [];
    this.windowSize = 80; // larger window for smoother data
    this.baselineVariance = null;
    this.threshold = 2.0; // z-score threshold - increased sensitivity
    this.isCalibrating = true;
    this.calibrationTime = 20000; // 20 seconds for stable baseline
    this.io = null;
    this.lastStatus = null;
    this.motionCount = 0; // hysteresis counter
    this.motionThreshold = 3; // require 3 consecutive detections for walking
    this.noMotionCount = 0;
    this.noMotionThreshold = 5; // require 5 consecutive non-detections to clear
  }

  async ping() {
    return new Promise((resolve, reject) => {
      exec(`ping -c 1 -t 1 ${this.routerIP}`, (error, stdout, stderr) => {
        if (error) {
          reject(error);
          return;
        }
        const match = stdout.match(/time=(\d+\.?\d*) ms/);
        if (match) {
          resolve(parseFloat(match[1]));
        } else {
          reject(new Error('No RTT found'));
        }
      });
    });
  }

  calculateVariance(data) {
    if (data.length < 2) return 0;
    const mean = math.mean(data);
    const variance = data.reduce((sum, val) => sum + Math.pow(val - mean, 2), 0) / data.length;
    return variance;
  }

  detectMotion(currentVariance) {
    if (this.baselineVariance === null) return false;
    const zScore = (currentVariance - this.baselineVariance) / Math.sqrt(this.baselineVariance);
    const isMotion = zScore > this.threshold;
    
    // Hysteresis logic to smooth out false positives
    if (isMotion) {
      this.motionCount++;
      this.noMotionCount = 0;
      return this.motionCount >= this.motionThreshold;
    } else {
      this.noMotionCount++;
      if (this.noMotionCount >= this.noMotionThreshold) {
        this.motionCount = 0;
      }
      return this.motionCount > 0; // still report motion until cleared
    }
  }

  start(io) {
    this.io = io;
    console.log('Starting WiFi motion detection...');
    console.log(`Pinging ${this.routerIP} every ${this.interval}ms`);

    setTimeout(() => {
      this.isCalibrating = false;
      this.baselineVariance = this.calculateVariance(this.rtts);
      console.log(`Calibration complete. Baseline variance: ${this.baselineVariance.toFixed(2)}`);
      if (this.io) {
        this.io.emit('calibrated', { baseline: this.baselineVariance });
      }
    }, this.calibrationTime);

    setInterval(async () => {
      try {
        const rtt = await this.ping();
        this.rtts.push(rtt);
        if (this.rtts.length > this.windowSize) {
          this.rtts.shift();
        }

        if (!this.isCalibrating) {
          const currentVariance = this.calculateVariance(this.rtts);
          const motion = this.detectMotion(currentVariance);
          if (motion) {
            const timestamp = new Date().toLocaleString();
            console.log(`Motion detected! Variance: ${currentVariance.toFixed(2)}, Baseline: ${this.baselineVariance.toFixed(2)}`);
            if (this.io) {
              this.io.emit('motion', {
                target: this.routerIP,
                message: this.routerIP === '192.168.0.180' ? 'LILY MOVING' : `Motion detected on ${this.routerIP}`,
                timestamp,
                variance: currentVariance,
                baseline: this.baselineVariance,
              });
            }
            this.lastStatus = 'motion';
          } else {
            if (this.lastStatus !== 'signal') {
              const timestamp = new Date().toLocaleString();
              console.log(`Signal detected with no movement on ${this.routerIP}`);
              if (this.io) {
                this.io.emit('signal', {
                  target: this.routerIP,
                  message: this.routerIP === '192.168.0.180' ? 'Signal detected, no movement' : `Signal detected on ${this.routerIP} with no movement`,
                  timestamp,
                  variance: currentVariance,
                  baseline: this.baselineVariance,
                });
              }
              this.lastStatus = 'signal';
            }
          }
        }
      } catch (error) {
        // console.error('Ping failed:', error.message);
      }
    }, this.interval);
  }
}

// Setup server
const app = express();
const server = http.createServer(app);
const io = socketIo(server);

app.use(express.static('public'));

app.get('/scan', async (req, res) => {
  try {
    const scanner = new NetworkScanner();
    const target = req.query.target;
    if (target) {
      const reachable = await scanner.pingDevice(target);
      res.json({ target, reachable });
      return;
    }
    const devices = await scanner.scanAndPing();
    res.json(devices);
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

const detector = new MotionDetector();

io.on('connection', (socket) => {
  console.log('Client connected');
  socket.on('disconnect', () => {
    console.log('Client disconnected');
  });
});

detector.start(io);

const PORT = process.env.PORT || 3000;
server.listen(PORT, '0.0.0.0', () => {
  console.log(`Server running on http://0.0.0.0:${PORT}`);
});