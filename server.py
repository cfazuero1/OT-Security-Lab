# server.py
import asyncio
import json
import socket
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

UDP_HOST = "0.0.0.0"
UDP_PORT = 9999

latest_state = {}

def udp_listener():
    global latest_state
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((UDP_HOST, UDP_PORT))
    while True:
        data, _ = sock.recvfrom(65535)
        try:
            latest_state = json.loads(data.decode("utf-8"))
        except Exception:
            # ignore malformed packets
            pass

app = FastAPI()

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

@app.get("/")
def index():
    return HTMLResponse((static_dir / "index.html").read_text(encoding="utf-8"))

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            if latest_state:
                await ws.send_text(json.dumps(latest_state))
            await asyncio.sleep(0.05)  # ~20 Hz
    except Exception:
        pass

@app.on_event("startup")
async def on_startup():
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, udp_listener)
