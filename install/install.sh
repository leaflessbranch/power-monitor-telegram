#!/bin/bash

# Power Monitor Installation Script
# This script installs the power monitor service on a Linux system

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
INSTALL_DIR="/opt/power-monitor"
SERVICE_NAME="power-monitor"
CONFIG_DIR="/etc/power-monitor"
LOG_DIR="/var/log"
DATA_DIR="/var/lib/power_monitor"

print_status() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

check_root() {
    if [[ $EUID -ne 0 ]]; then
        print_error "This script must be run as root"
        exit 1
    fi
}

install_dependencies() {
    print_status "Installing system dependencies..."
    
    # Update package list
    apt-get update
    
    # Install required packages
    apt-get install -y python3 python3-pip python3-venv git
    
    print_status "System dependencies installed successfully"
}

create_directories() {
    print_status "Creating directories..."
    
    mkdir -p "$INSTALL_DIR"
    mkdir -p "$CONFIG_DIR"
    mkdir -p "$DATA_DIR"
    
    # Set permissions
    chown root:root "$INSTALL_DIR"
    chmod 755 "$INSTALL_DIR"
    
    chown root:root "$CONFIG_DIR"
    chmod 755 "$CONFIG_DIR"
    
    chown root:root "$DATA_DIR"
    chmod 755 "$DATA_DIR"
    
    print_status "Directories created successfully"
}

install_application() {
    print_status "Installing power monitor application..."
    
    # Copy application files
    cp power_monitor.py "$INSTALL_DIR/"
    cp requirements.txt "$INSTALL_DIR/"
    
    # Copy example configuration files
    cp examples/devices.json.example "$CONFIG_DIR/devices.json"
    cp examples/power-monitor.env.example "$CONFIG_DIR/power-monitor.env"
    
    # Set permissions
    chmod 644 "$INSTALL_DIR/power_monitor.py"
    chmod 644 "$INSTALL_DIR/requirements.txt"
    chmod 644 "$CONFIG_DIR/devices.json"
    chmod 600 "$CONFIG_DIR/power-monitor.env"  # Secure permissions for env file
    
    print_status "Application files installed successfully"
}

create_virtual_environment() {
    print_status "Creating Python virtual environment..."
    
    cd "$INSTALL_DIR"
    python3 -m venv venv
    
    # Activate virtual environment and install dependencies
    source venv/bin/activate
    pip install --upgrade pip
    pip install -r requirements.txt
    
    print_status "Virtual environment created and dependencies installed"
}

install_systemd_service() {
    print_status "Installing systemd service..."
    
    # Copy service file
    cp power-monitor.service /etc/systemd/system/
    
    # Reload systemd
    systemctl daemon-reload
    
    # Enable service (but don't start it yet)
    systemctl enable "$SERVICE_NAME"
    
    print_status "Systemd service installed successfully"
}

print_next_steps() {
    echo
    print_status "Installation completed successfully!"
    echo
    print_warning "IMPORTANT: Before starting the service, you need to:"
    echo "1. Configure your Telegram bot token and chat ID in: $CONFIG_DIR/power-monitor.env"
    echo "2. Update the monitored devices in: $CONFIG_DIR/devices.json"
    echo
    print_status "Configuration files:"
    echo "  - Environment variables: $CONFIG_DIR/power-monitor.env"
    echo "  - Device configuration: $CONFIG_DIR/devices.json"
    echo
    print_status "Service management commands:"
    echo "  - Start service: sudo systemctl start $SERVICE_NAME"
    echo "  - Stop service: sudo systemctl stop $SERVICE_NAME"
    echo "  - View logs: sudo journalctl -u $SERVICE_NAME -f"
    echo "  - Check status: sudo systemctl status $SERVICE_NAME"
    echo
    print_status "For more information, visit: https://github.com/leaflessbranch/power-monitor-telegram"
}

main() {
    echo "Power Monitor Installation Script"
    echo "================================="
    echo
    
    check_root
    install_dependencies
    create_directories
    install_application
    create_virtual_environment
    install_systemd_service
    print_next_steps
}

main "$@"