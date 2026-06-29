# Dashboard

`dashboard.py` provides a full-screen terminal dashboard with sparklines, gauges, charger configuration, and time-to-full / time-to-empty estimates.

## Running

```bash
python3 dashboard.py
```

The dashboard subscribes to MQTT for live state. It does **not** open the RS-485 port, so it can run alongside `solartracer-mqtt.service` without serial lock contention.

## Fallback data

When MQTT is unavailable, the dashboard falls back to local history files in `var/` (voltage trend and battery rate log).

## Time estimates

Estimates combine:

- LiFePO4 open-circuit voltage curve and plateau handling
- Voltage trend over a rolling window
- Empirical charge/discharge rates from 3/10/30/60 minute lookbacks
- Prior-day rate grounding from `var/battery-rate-log.json`

Warmup countdown labels appear while the voltage trend window is still collecting samples.