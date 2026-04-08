const { exec } = require('child_process');

class NetworkScanner {
  constructor(networkPrefix = '192.168.0') {
    this.networkPrefix = networkPrefix;
  }

  async scan() {
    return new Promise((resolve, reject) => {
      // Use arp -a to get ARP table, or nmap if available
      exec('arp -a', (error, stdout, stderr) => {
        if (error) {
          reject(error);
          return;
        }

        const devices = [];
        const lines = stdout.split('\n');
        for (const line of lines) {
          const match = line.match(/\((\d+\.\d+\.\d+\.\d+)\) at ([a-f0-9:]+)/i);
          if (match) {
            const ip = match[1];
            const mac = match[2];
            if (ip.startsWith(this.networkPrefix)) {
              devices.push({ ip, mac });
            }
          }
        }
        resolve(devices);
      });
    });
  }

  async pingDevice(ip) {
    return new Promise((resolve) => {
      exec(`ping -c 1 -t 1 ${ip}`, (error, stdout, stderr) => {
        const reachable = !error;
        resolve(reachable);
      });
    });
  }

  async scanAndPing() {
    const devices = await this.scan();
    const reachableDevices = [];
    for (const device of devices) {
      const reachable = await this.pingDevice(device.ip);
      if (reachable) {
        reachableDevices.push(device);
      }
    }
    return reachableDevices;
  }
}

// Usage
if (require.main === module) {
  const scanner = new NetworkScanner();
  scanner.scanAndPing().then(devices => {
    console.log('Reachable devices:');
    devices.forEach(device => {
      console.log(`${device.ip} - ${device.mac}`);
    });
  }).catch(console.error);
}

module.exports = NetworkScanner;