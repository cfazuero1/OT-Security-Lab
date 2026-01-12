#!/usr/bin/env python3
import time
import threading
from dataclasses import dataclass

from pymodbus.server import StartTcpServer
from pymodbus.datastore import (
    ModbusServerContext,
    ModbusSlaveContext,
    ModbusSequentialDataBlock,
)
from pymodbus.device import ModbusDeviceIdentification

HOST = "0.0.0.0"
PORT = 5020
UNIT_ID = 1
SCAN_TIME_SEC = 0.10

# -----------------------------
# Address Map
# -----------------------------
# Coils
CO_TURNOUT_MAIN = 0   # 1=MAIN, 0=DIV/SIDING
CO_ESTOP = 1          # 1=STOP, 0=RUN

# Holding registers (signals) - NOW BINARY
# 0 = RED, 1 = GREEN
HR_SIG_AB = 0
HR_SIG_BC = 1
HR_SIG_SB = 2

# Holding registers (inputs / sensor injection)
HR_IN_OCC_A = 100
HR_IN_OCC_B = 101
HR_IN_OCC_C = 102
HR_IN_CRASH = 103

# NEW: Mode register
# 0 = AUTO (PLC computes signals)
# 1 = MANUAL (SCADA controls HR[0..2] and HR[100..103])
HR_MODE = 50


def b(v) -> int:
    return 1 if int(v) != 0 else 0


def clamp01(v) -> int:
    return 1 if int(v) != 0 else 0


def hr_get(store: ModbusSlaveContext, addr: int) -> int:
    return int(store.getValues(3, addr, count=1)[0])  # 3 = HR


def hr_set(store: ModbusSlaveContext, addr: int, value: int) -> None:
    store.setValues(3, addr, [int(value)])


def co_get(store: ModbusSlaveContext, addr: int) -> int:
    return int(store.getValues(1, addr, count=1)[0])  # 1 = coils


def co_set(store: ModbusSlaveContext, addr: int, value: int) -> None:
    store.setValues(1, addr, [b(value)])


@dataclass
class Inputs:
    occA: int
    occB: int
    occC: int
    crash: int


def read_inputs(store: ModbusSlaveContext) -> Inputs:
    return Inputs(
        occA=b(hr_get(store, HR_IN_OCC_A)),
        occB=b(hr_get(store, HR_IN_OCC_B)),
        occC=b(hr_get(store, HR_IN_OCC_C)),
        crash=b(hr_get(store, HR_IN_CRASH)),
    )


def plc_logic_scan(store: ModbusSlaveContext) -> None:
    mode = clamp01(hr_get(store, HR_MODE))
    ins = read_inputs(store)

    # Crash -> force stop + all red
    if ins.crash:
        co_set(store, CO_ESTOP, 1)
        hr_set(store, HR_SIG_AB, 0)
        hr_set(store, HR_SIG_BC, 0)
        hr_set(store, HR_SIG_SB, 0)
        return

    co_set(store, CO_ESTOP, 0)

    # Turnout command from coil 0, but lock to MAIN if junction occupied (safety)
    turnout_main = 1 if co_get(store, CO_TURNOUT_MAIN) else 0
    if ins.occB:
        turnout_main = 1
        co_set(store, CO_TURNOUT_MAIN, 1)

    # -----------------------------
    # MODE = 1 (MANUAL) => SCADA controls signals
    # Do NOT overwrite HR[0..2]
    # -----------------------------
    if mode == 1:
        # Optionally clamp to 0/1 (keeps it clean)
        hr_set(store, HR_SIG_AB, clamp01(hr_get(store, HR_SIG_AB)))
        hr_set(store, HR_SIG_BC, clamp01(hr_get(store, HR_SIG_BC)))
        hr_set(store, HR_SIG_SB, clamp01(hr_get(store, HR_SIG_SB)))
        return

    # -----------------------------
    # MODE = 0 (AUTO) => PLC computes signals (binary interlock)
    # 0=RED, 1=GREEN
    # -----------------------------
    # A<->B signal green only if B is free
    sig_ab = 1 if ins.occB == 0 else 0

    # B<->C signal green only if C is free
    sig_bc = 1 if ins.occC == 0 else 0

    # S<->B signal green only if:
    # - turnout in MAIN
    # - B is free
    sig_sb = 1 if (turnout_main == 1 and ins.occB == 0) else 0

    hr_set(store, HR_SIG_AB, sig_ab)
    hr_set(store, HR_SIG_BC, sig_bc)
    hr_set(store, HR_SIG_SB, sig_sb)


def scan_loop(context: ModbusServerContext):
    last_print = 0.0
    while True:
        store = context[UNIT_ID]
        plc_logic_scan(store)

        now = time.time()
        if now - last_print > 1.0:
            last_print = now
            ins = read_inputs(store)
            turnout = co_get(store, CO_TURNOUT_MAIN)
            estop = co_get(store, CO_ESTOP)
            mode = hr_get(store, HR_MODE)
            print(
                f"[PLC] mode={mode} A={ins.occA} B={ins.occB} C={ins.occC} crash={ins.crash} | "
                f"turnout={'MAIN' if turnout else 'DIV'} estop={estop} | "
                f"SigAB={hr_get(store, HR_SIG_AB)} SigBC={hr_get(store, HR_SIG_BC)} SigSB={hr_get(store, HR_SIG_SB)}"
            )

        time.sleep(SCAN_TIME_SEC)


def main():
    store = ModbusSlaveContext(
        di=ModbusSequentialDataBlock(0, [0] * 200),
        co=ModbusSequentialDataBlock(0, [0] * 200),
        hr=ModbusSequentialDataBlock(0, [0] * 300),
        ir=ModbusSequentialDataBlock(0, [0] * 200),
        zero_mode=True,
    )

    # defaults
    co_set(store, CO_TURNOUT_MAIN, 1)
    co_set(store, CO_ESTOP, 0)

    hr_set(store, HR_MODE, 0)     # AUTO by default
    hr_set(store, HR_SIG_AB, 0)
    hr_set(store, HR_SIG_BC, 0)
    hr_set(store, HR_SIG_SB, 0)

    # sensor injection defaults
    hr_set(store, HR_IN_OCC_A, 0)
    hr_set(store, HR_IN_OCC_B, 0)
    hr_set(store, HR_IN_OCC_C, 0)
    hr_set(store, HR_IN_CRASH, 0)

    context = ModbusServerContext(slaves={UNIT_ID: store}, single=False)

    identity = ModbusDeviceIdentification()
    identity.VendorName = "RailwayLab"
    identity.ProductCode = "RWPLC"
    identity.ProductName = "Railway Modbus PLC Simulator"
    identity.ModelName = "RW-PLC-1"
    identity.MajorMinorRevision = "2.0"

    t = threading.Thread(target=scan_loop, args=(context,), daemon=True)
    t.start()

    print(f"[PLC] Modbus TCP server listening on {HOST}:{PORT} unit={UNIT_ID}")
    print(f"[PLC] HR[90]=MODE (0=AUTO,1=MANUAL). Signals: 0=RED 1=GREEN")
    StartTcpServer(context=context, identity=identity, address=(HOST, PORT))


if __name__ == "__main__":
    main()

