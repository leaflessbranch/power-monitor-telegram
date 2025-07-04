# Power Monitor Telegram Bot

A Python-based power outage monitoring system that detects power cuts by monitoring network-connected devices and sends real-time notifications via Telegram.

## Features

- **Real-time monitoring**: Continuously monitors power status by pinging smart devices
- **Telegram notifications**: Instant alerts when power goes out or is restored
- **Historical tracking**: SQLite database stores power outage history with statistics
- **Interactive bot**: Telegram bot with commands to check status and view history
- **Automatic recovery**: Handles unexpected shutdowns and system restarts gracefully
- **Configurable devices**: Monitor any network-connected devices (smart plugs, routers, etc.)
- **Systemd service**: Runs as a background service with automatic restart

## How It Works

The system monitors power status by pinging network-connected devices (smart plugs, bulbs, routers) at regular intervals. When all monitored devices become unreachable, it assumes a power outage has occurred and sends a Telegram notification. When devices become reachable again, it detects power restoration and logs the outage duration.

## Prerequisites

- Linux system with Python 3.6+
- Network-connected devices to monitor (smart plugs, routers, etc.)
- Telegram bot token and chat ID
- Root access for system service installation

## Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/leaflessbranch/power-monitor-telegram.git
cd power-monitor-telegram
```

### 2. Set Up Telegram Bot

1. Create a new bot by messaging [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts to get your bot token
3. Get your chat ID by messaging [@userinfobot](https://t.me/userinfobot) or by sending a message to your bot and checking the Telegram API

### 3. Install (Option A: Automatic)

Run the installation script as root:

```bash
sudo ./install/install.sh
```

### 4. Install (Option B: Manual)

```bash
# Create directories
sudo mkdir -p /opt/power-monitor /etc/power-monitor /var/lib/power_monitor

# Copy files
sudo cp power_monitor.py /opt/power-monitor/
sudo cp requirements.txt /opt/power-monitor/
sudo cp examples/devices.json.example /etc/power-monitor/devices.json
sudo cp examples/power-monitor.env.example /etc/power-monitor/power-monitor.env
sudo cp power-monitor.service /etc/systemd/system/

# Create virtual environment and install dependencies
cd /opt/power-monitor
sudo python3 -m venv venv
sudo venv/bin/pip install -r requirements.txt

# Enable service
sudo systemctl daemon-reload
sudo systemctl enable power-monitor
```

### 5. Configure

Edit the configuration files:

```bash
# Configure Telegram credentials
sudo nano /etc/power-monitor/power-monitor.env

# Configure monitored devices
sudo nano /etc/power-monitor/devices.json
```

### 6. Start the Service

```bash
sudo systemctl start power-monitor
sudo systemctl status power-monitor
```

## Configuration

### Environment Variables

Edit `/etc/power-monitor/power-monitor.env`:

```bash
# Required: Get from @BotFather
TELEGRAM_BOT_TOKEN=your_bot_token_here

# Required: Get from @userinfobot
TELEGRAM_CHAT_ID=your_chat_id_here
```

### Device Configuration

Edit `/etc/power-monitor/devices.json`:

```json
{
  "monitored_devices": [
    {
      "name": "Smart Plug Living Room",
      "ip": "192.168.1.100",
      "description": "TP-Link Kasa Smart Plug"
    },
    {
      "name": "WiFi Router",
      "ip": "192.168.1.1",
      "description": "Main router - reliable power indicator"
    }
  ]
}
```

**Device Selection Tips:**
- Use devices that are always powered on (smart plugs, routers, always-on bulbs)
- Avoid devices that might be turned off manually
- Include multiple devices for reliability
- Ensure devices are on the same network as your monitoring server

## Telegram Bot Commands

The bot supports these commands:

- `/status` - Check current power status
- `/history` - View power outage history (last 30 days) with pagination
- `/fix` - Fix stuck states if status appears incorrect
- `/help` - Show help message
- `/start` - Welcome message and instructions

Interactive buttons are provided for easy access to these commands. The history command now includes pagination with Previous/Next buttons to navigate through all records when there are more than 10 entries.

## Service Management

```bash
# Start the service
sudo systemctl start power-monitor

# Stop the service
sudo systemctl stop power-monitor

# Restart the service
sudo systemctl restart power-monitor

# Check service status
sudo systemctl status power-monitor

# View logs
sudo journalctl -u power-monitor -f

# Enable auto-start on boot
sudo systemctl enable power-monitor

# Disable auto-start
sudo systemctl disable power-monitor
```

## File Locations

- **Application**: `/opt/power-monitor/`
- **Configuration**: `/etc/power-monitor/`
- **Database**: `/var/lib/power_monitor/power_cuts.db`
- **Logs**: `/var/log/power_monitor.log`
- **Service**: `/etc/systemd/system/power-monitor.service`

## Troubleshooting

### Service Won't Start

1. Check the service status:
   ```bash
   sudo systemctl status power-monitor
   ```

2. Check logs for errors:
   ```bash
   sudo journalctl -u power-monitor -f
   ```

3. Verify configuration:
   ```bash
   sudo cat /etc/power-monitor/power-monitor.env
   ```

### Bot Not Responding

1. Verify Telegram credentials in `/etc/power-monitor/power-monitor.env`
2. Test bot token with a curl command:
   ```bash
   curl https://api.telegram.org/bot<YOUR_TOKEN>/getMe
   ```
3. Check if chat ID is correct

### False Positives

1. Adjust device list to include more reliable devices
2. Check network connectivity to monitored devices
3. Use `/fix` command to reset stuck states

### Database Issues

Reset the database if needed:
```bash
sudo rm /var/lib/power_monitor/power_cuts.db
sudo systemctl restart power-monitor
```

## Development

### Local Development

```bash
# Clone repository
git clone https://github.com/leaflessbranch/power-monitor-telegram.git
cd power-monitor-telegram

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy example configuration
cp examples/power-monitor.env.example .env
cp examples/devices.json.example devices.json

# Edit configuration files
nano .env
nano devices.json

# Run locally (modify paths in script for local testing)
python power_monitor.py
```

### Configuration for Local Testing

For local development, you may want to modify the file paths in `power_monitor.py`:

```python
CONFIG = {
    "db_path": "./power_cuts.db",  # Local database
    "log_path": "./power_monitor.log",  # Local log file
    # ... other config
}
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Support

If you encounter issues or have questions:

1. Check the [troubleshooting section](#troubleshooting)
2. Review the logs: `sudo journalctl -u power-monitor -f`
3. Open an issue on [GitHub](https://github.com/leaflessbranch/power-monitor-telegram/issues)

## Disclaimer

This software is provided as-is for monitoring purposes. It should not be used as the sole method for critical power monitoring in production environments. Always have backup monitoring systems for critical infrastructure.