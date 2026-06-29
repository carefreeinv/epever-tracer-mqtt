# Getting started

## Clone and install dependencies

```bash
git clone https://github.com/carefreeinv/epever-tracer-mqtt.git
cd epever-tracer-mqtt
pip3 install -r requirements.txt
cp .env.example .env
# edit .env with your MQTT broker settings
```

## Configure the Exar adapter

The XR21B1411 USB stick must be placed in RS-485 half-duplex mode before Modbus will work:

```bash
sudo python3 configure-exar-rs485.py
```

After installing the udev rule (see [Installation](installation.md)), this runs automatically on USB plug. You still need to run it manually once after the first clone.

## Test a Modbus read

```bash
python3 solar_data.py
```

You should see battery voltage, PV readings, and charger status fields printed to the terminal.

## Run the MQTT publisher

Foreground (for testing):

```bash
python3 publish_mqtt.py
```

Test without a broker:

```bash
DRY_RUN=1 python3 publish_mqtt.py
```

## Optional dashboard

The terminal dashboard reads live state from MQTT and does not open the serial port directly:

```bash
python3 dashboard.py
```

It can run alongside the systemd MQTT service without RS-485 lock contention.

## Next steps

- [Install as a systemd service](installation.md)
- [Configure environment variables](configuration.md)
- [Set up Home Assistant entities](home-assistant.md)