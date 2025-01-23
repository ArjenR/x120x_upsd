#!/bin/sh
apt install -y python3-apscheduler python3-decorator python3-gpiozero \
    python3-smbus2 python3-systemd

cp x120x_upsd.py /usr/local/bin
cp -n x120x_upsd.ini /usr/local/etc || true
cp x120x_upsd.service /etc/systemd/system

chown root:root /usr/local/bin/x120x_upsd.py /usr/local/etc/x120x_upsd.ini /etc/systemd/system/x120x_upsd.service
chmod 644 /usr/local/etc/x120x_upsd.ini /etc/systemd/system/x120x_upsd.service
chmod 755 /usr/local/bin/x120x_upsd.py

systemctl daemon-reload
systemctl enable x120x_upsd.service
systemctl start x120x_upsd.service
