# Troubleshooting

## Charger shows unreachable in Home Assistant

Check service logs:

```bash
sudo journalctl -u solartracer-mqtt.service -n 50 --no-pager
```

Look for:

- `Charger unreachable` warnings with `present=` / `missing=` register detail
- `[Errno 5] Input/output error` on `/dev/solartracer-rs485` — USB driver problem
- `Serial recovery:` lines — automatic recovery attempts

### Quick connectivity test

```bash
python3 -c "from solar_data import probe_charger; print(probe_charger('/dev/solartracer-rs485'))"
```

`True` means Modbus is responding. `False` means the port or bus is down.

## USB adapter not found

```bash
ls -la /dev/solartracer-rs485 /dev/ttyACM*
lsusb | grep -i exar
```

If `lsusb` shows the Exar (`04e2:1411`) but no `ttyACM` node, the `cdc_acm` driver failed to bind. Check kernel messages:

```bash
dmesg | tail -30
```

Common errors:

- `acm_port_activate - usb_submit_urb(ctrl irq) failed`
- `can't set config #1, error -32`

**Fix:** physically unplug and replug the USB adapter, then:

```bash
sudo python3 configure-exar-rs485.py
sudo systemctl restart solartracer-mqtt.service
```

## systemd service won't start

If you see `Unit has a bad unit file setting`, the installed unit still has template placeholders. Re-run:

```bash
./scripts/install.sh
```

## Serial permission denied

```bash
groups    # should include dialout
sudo usermod -aG dialout "$USER"
```

Log out and back in after adding the group.

## Recovery hammering the USB driver

The publisher defers full Exar reconfiguration until 3 consecutive failures. If you are on an older version that reconfigured on every miss, upgrade to v0.4.0+.

The `configure-exar-rs485.py --gentle` flag pokes RS-485 registers without unloading `cdc_acm`. Full reconfigure (with driver reload) should only run after USB replug when the tty node is missing.

## Debug logging

Enable verbose journal output:

```bash
# in .env
SOLARTRACER_DEBUG=1
```

Then restart the service. Logs include per-register read failures, recovery steps, and poll cycle mode.

## MQTT states cleared unexpectedly

After 3 consecutive unreachable reads, retained MQTT topics are cleared so Home Assistant does not show stale values. This is intentional. States repopulate automatically when comms restore.