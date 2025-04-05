#!/bin/bash

set -e

SERVICE_NAME=billboard
APP_DIR=/opt/$SERVICE_NAME
VENV_DIR=$APP_DIR/venv
SCRIPT_NAME=billboard.py
SYSTEMD_FILE=/etc/systemd/system/$SERVICE_NAME.service

# 1. Create application directory
sudo mkdir -p "$APP_DIR"
sudo cp "$SCRIPT_NAME" "$APP_DIR/"

# 2. Create virtualenv and install packages
sudo python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install --upgrade pip
pip install -r requirements.txt

# 3. Create systemd service file
sudo tee "$SYSTEMD_FILE" > /dev/null <<EOF
[Unit]
Description=Image Display Network Daemon
After=network.target

[Service]
ExecStart=$VENV_DIR/bin/python $APP_DIR/$SCRIPT_NAME
WorkingDirectory=$APP_DIR
Restart=on-failure
User=pi
Group=pi
StandardOutput=append:/var/log/image_display_daemon.log
StandardError=append:/var/log/image_display_daemon.log
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

# 4. Reload systemd and enable the service
sudo systemctl daemon-reexec
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

# 5. Start the service
sudo systemctl start "$SERVICE_NAME"
echo "$SERVICE_NAME installed and started."