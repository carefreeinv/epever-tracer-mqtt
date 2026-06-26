#!/usr/bin/env python3
"""
Configure the Exar XR21B1411 USB RS-485 stick for proper half-duplex
signaling with EPEVER / Tracer BN solar controllers.

This replicates what the old Windows custom driver (and the xr_usb_serial_common
Linux driver) do: set gpio_mode to 0x0b (RS-485) and related registers.

Run as root:
  sudo python3 configure-exar-rs485.py

A udev rule can also call this on device plug so it happens automatically.
After configuration, /dev/ttyACM0 will work for Modbus with the controller.
"""
import sys
import os
import time
import usb.core

VID = 0x04e2
PID = 0x1411

def rebind_cdc_acm(dev):
    """Try to make the kernel recreate /dev/ttyACM0 after we touched interfaces."""
    # Common location on this hardware. Use sysfs unbind/bind on the USB device.
    # We look for any "1-*" or use the bus/port info.
    # A simple reliable way: unload + reload cdc_acm (affects only ACM devices)
    print("  ensuring cdc_acm is bound (ttyACM node)...")
    os.system("modprobe cdc_acm 2>/dev/null || true")
    # Try to force a rebind of this specific device
    # Find the device path under /sys/bus/usb/devices/
    for root, dirs, files in os.walk("/sys/bus/usb/devices"):
        for d in dirs:
            if d.startswith(str(dev.bus) + "-"):
                devpath = os.path.join(root, d)
                # Try to rebind the whole usb device
                try:
                    with open(os.path.join(devpath, "uevent"), "w") as f:
                        pass
                except:
                    pass
                # unbind/bind dance on the usb driver for this device
                try:
                    with open("/sys/bus/usb/drivers/usb/unbind", "w") as f:
                        f.write(d + "\n")
                except:
                    pass
                time.sleep(0.8)
                try:
                    with open("/sys/bus/usb/drivers/usb/bind", "w") as f:
                        f.write(d + "\n")
                except:
                    pass
                time.sleep(1.5)
                return

def main():
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        print("Exar XR21B1411 not found")
        return 1

    print(f"Found Exar XR21B1411 (bus {dev.bus} addr {dev.address})")

    # First, try the pokes WITHOUT detaching. Control transfers on ep0 often
    # succeed even when cdc_acm owns the interfaces. This avoids killing ttyACM0.
    detached = False
    SET_REQ = 0
    SET_TYPE = 0x40

    def set_reg(reg, val):
        try:
            dev.ctrl_transfer(SET_TYPE, SET_REQ, val, reg, None, timeout=4000)
            return True
        except Exception:
            return False

    # Try non-intrusive first
    if not (set_reg(0x20d, 0x01) and set_reg(0xc0c, 0x0b)):
        print("  direct pokes failed, detaching kernel driver temporarily...")
        for cfg in dev:
            for intf in cfg:
                ifnum = intf.bInterfaceNumber
                if dev.is_kernel_driver_active(ifnum):
                    try:
                        dev.detach_kernel_driver(ifnum)
                        detached = True
                    except Exception:
                        pass
        try:
            dev.set_configuration()
        except Exception:
            pass
        # Now poke
        set_reg(0x20d, 0x01)
        set_reg(0xc0d, 0x28)
        set_reg(0xc0c, 0x0b)
        set_reg(0xc00, 0x03)

    # Always (re)apply the important values
    set_reg(0x20d, 0x01)
    set_reg(0xc0d, 0x28)
    set_reg(0xc0c, 0x0b)
    set_reg(0xc00, 0x03)

    # The pokes (especially with detach) usually remove the ttyACM0.
    # Force cdc_acm to re-attach to the (already configured) device.
    print("  re-attaching cdc_acm driver so /dev/ttyACM0 reappears...")
    os.system("rmmod cdc_acm 2>/dev/null || true")
    time.sleep(0.3)
    os.system("modprobe cdc_acm 2>/dev/null || true")
    time.sleep(1.2)

    print("RS-485 signaling mode activated (gpio_mode=0x0b).")
    print("Use /dev/ttyACM0 with 115200 8N1 RTU (slave 1).")
    return 0

if __name__ == "__main__":
    sys.exit(main())
