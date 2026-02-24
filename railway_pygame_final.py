#!/usr/bin/env python3
"""
railway_pygame.py
2D Railway OT Demo (Pygame) + Modbus TCP "PLC" memory (ScadaBR friendly)

Key features (your requirements):
- Signals are BINARY: GREEN=1, RED=0 (no yellow).
- Game starts with ALL lights RED.
- T1 has priority over T2 in AUTO (junction headway lock).
- AUTO cycle:
    1) Initial WAIT 3s (no Modbus traffic).
    2) T1 goes A -> B -> C.
    3) T2 goes S -> B, waits 3s, returns B -> S.
    4) T1 returns C -> B -> A, waits 3s.
    5) Repeat.
- Interlocking-like signalling:
    - While a movement is authorised, the relevant signal is GREEN.
    - Once the train leaves the governed segment, the signal drops back to RED.

WAIT windows:
- During every 3s WAIT, the game stops ALL Modbus reads/writes,
  so SCADA can safely push overrides without being overwritten.

AUTO / MANUAL modes:
- Press M to toggle AUTO<->MANUAL locally.
- SCADA can also request MANUAL by writing HR[50]=1.
- In MANUAL, the game does NOT write signals/occupancy.
  On resume from WAIT (or switching MANUAL->AUTO), the game re-syncs from server and
  applies SCADA overrides (signals + occupancy), allowing SCADA to create collisions.

NEW crash behaviour (your latest requirement):
- If a collision occurs:
    * Manual is forcibly disabled (local + SCADA manual ignored)
    * Game writes HR[103]=1 to PLC immediately (even if comms are normally paused for WAIT)
    * After crash animation, reset back to normal and continue.

Modbus memory map:
  Holding registers written by GAME (AUTO only):
    HR[100] = occA (0/1)
    HR[101] = occB (0/1)
    HR[102] = occC (0/1)
    HR[103] = crash (0/1)

  Holding registers written by SCADA (and read by GAME):
    HR[0]   = SigAB (0=RED, 1=GREEN)
    HR[1]   = SigBC (0=RED, 1=GREEN)
    HR[2]   = SigSB (0=RED, 1=GREEN)
    HR[50]  = SCADA manual request (0=AUTO, 1=MANUAL)
    HR[110] = override occA (0/1)
    HR[111] = override occB (0/1)
    HR[112] = override occC (0/1)
    HR[113] = override occS (0/1)

  Coils (written by SCADA and read by GAME):
    COIL[0] = turnout_main (1=MAIN, 0=SIDING/DIVERGE)
    COIL[1] = estop (1=STOP MOVEMENT)

Controls:
  M     Toggle local AUTO/MANUAL
  T     Toggle turnout coil (writes PLC)  (works only when Modbus comms enabled)
  SPACE Reset simulation
  ESC   Quit
"""

import sys
import time
import math
import pygame

import json
import socket
# ---------------- Modbus (PLC) integration ----------------
PLC_HOST = "127.0.0.1"
PLC_PORT = 5020
PLC_UNIT = 1
PLC_POLL_SEC = 0.10  # 10 Hz

# Inputs written by GAME (AUTO only)
HR_IN_OCC_A = 100
HR_IN_OCC_B = 101
HR_IN_OCC_C = 102
HR_IN_CRASH = 103

# Outputs / commands written by SCADA
HR_SIG_AB = 0
HR_SIG_BC = 1
HR_SIG_SB = 2
HR_MODE = 50  # 0=AUTO, 1=MANUAL
HR_OVR_OCC_BASE = 110  # 110..113 => A,B,C,S overrides

# Coils
CO_TURNOUT_MAIN = 0
CO_ESTOP = 1

try:
    from pymodbus.client import ModbusTcpClient
except Exception:
    ModbusTcpClient = None


class PlcClient:
    """pymodbus v3 prefers slave=; older versions used unit=. We try slave then unit."""
    def __init__(self, host: str, port: int, unit_id: int = 1):
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.client = None
        self.connected = False

    def _kw(self, prefer_slave=True):
        return {"slave": self.unit_id} if prefer_slave else {"unit": self.unit_id}

    def connect(self) -> bool:
        if ModbusTcpClient is None:
            print("[PLC] pymodbus not installed.")
            return False
        try:
            self.client = ModbusTcpClient(self.host, port=self.port)
            self.connected = bool(self.client.connect())
        except Exception as e:
            print(f"[PLC] connect failed {self.host}:{self.port} -> {e}")
            self.connected = False
        return self.connected

    def close(self):
        try:
            if self.client:
                self.client.close()
        except Exception:
            pass
        self.connected = False

    def read_coils_basic(self):
        """Return (turnout_main, estop) as ints."""
        turnout_main, estop = 1, 0
        if not self.connected:
            return turnout_main, estop
        try:
            rr = self.client.read_coils(CO_TURNOUT_MAIN, 2, **self._kw(prefer_slave=True))
            if rr and not rr.isError():
                turnout_main = int(rr.bits[0])
                estop = int(rr.bits[1])
        except TypeError:
            rr = self.client.read_coils(CO_TURNOUT_MAIN, 2, **self._kw(prefer_slave=False))
            if rr and not rr.isError():
                turnout_main = int(rr.bits[0])
                estop = int(rr.bits[1])
        except Exception:
            self.connected = False
        return turnout_main, estop

    def read_holding(self, addr: int, count: int):
        if not self.connected:
            return None
        try:
            rr = self.client.read_holding_registers(addr, count, **self._kw(prefer_slave=True))
            if rr and not rr.isError():
                return [int(v) for v in rr.registers]
        except TypeError:
            rr = self.client.read_holding_registers(addr, count, **self._kw(prefer_slave=False))
            if rr and not rr.isError():
                return [int(v) for v in rr.registers]
        except Exception:
            self.connected = False
        return None

    def write_registers(self, addr: int, values):
        if not self.connected:
            return False
        try:
            self.client.write_registers(addr, [int(v) for v in values], **self._kw(prefer_slave=True))
            return True
        except TypeError:
            self.client.write_registers(addr, [int(v) for v in values], **self._kw(prefer_slave=False))
            return True
        except Exception:
            self.connected = False
            return False

    def write_inputs(self, occA, occB, occC, crash):
        """AUTO only: publish occupancy/crash to PLC as holding registers 100..103."""
        return self.write_registers(HR_IN_OCC_A, [occA, occB, occC, crash])

    def write_signals(self, sig_ab, sig_bc, sig_sb):
        """AUTO only: publish commanded signals to PLC as holding registers 0..2."""
        return self.write_registers(HR_SIG_AB, [sig_ab, sig_bc, sig_sb])

    def write_crash_only(self, crash_bit: int):
        """Force crash bit immediately (even during WAIT)."""
        return self.write_registers(HR_IN_CRASH, [1 if crash_bit else 0])

    def toggle_turnout(self):
        if not self.connected:
            return
        try:
            rr = self.client.read_coils(CO_TURNOUT_MAIN, 1, **self._kw(prefer_slave=True))
            if rr and not rr.isError():
                cur = int(rr.bits[0])
                self.client.write_coils(CO_TURNOUT_MAIN, [0 if cur else 1], **self._kw(prefer_slave=True))
        except TypeError:
            rr = self.client.read_coils(CO_TURNOUT_MAIN, 1, **self._kw(prefer_slave=False))
            if rr and not rr.isError():
                cur = int(rr.bits[0])
                self.client.write_coils(CO_TURNOUT_MAIN, [0 if cur else 1], **self._kw(prefer_slave=False))
        except Exception:
            self.connected = False


# ----------------- Pygame config -----------------
# ---------------- UDP state broadcast (for 3D viewer) ----------------
UDP_VIEWER_IP = "127.0.0.1"
UDP_VIEWER_PORT = 9999
UDP_SEND_HZ = 20  # viewer update rate

WIDTH, HEIGHT = 1040, 540
FPS = 60

PANEL_Y = 110
PANEL_H = 75
BLOCK_W = 180
BLOCK_GAP = 30

TRACK_Y_MAIN = 360
TRACK_Y_SIDING = 445
TRACK_LEFT_PAD = 70
TRACK_RIGHT_PAD = 70

RAIL_GAP = 18
RAIL_THICKNESS = 5
SLEEPER_EVERY = 28
SLEEPER_LEN = 32

MOVE_DURATION_SEC = 1.6

# Signals: RED=0, GREEN=1
RED, GREEN = 0, 1

LABEL_RIGHT_OFFSET = 22
LABEL_VERTICAL_NUDGE = -14
LABEL_BLOCK_CLEARANCE = 10

# Auto-cycle / timing
WAIT_SEC = 3.0
JUNCTION_HEADWAY_SEC = 1.2  # T1 priority lock-out window for T2 entering B

# Auto states
AUTO_INIT_WAIT = -1
AUTO_T1_A_TO_C = 0
AUTO_T2_S_TO_B = 1
AUTO_WAIT_AT_B = 2
AUTO_T2_B_TO_S = 3
AUTO_T1_C_TO_A = 4
AUTO_WAIT_AT_A = 5


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def lerp(a, b, t):
    return a + (b - a) * t


def draw_text(screen, font, text, x, y, color=(20, 20, 20)):
    surf = font.render(text, True, color)
    screen.blit(surf, (x, y))


def bit_color(v):
    return (35, 175, 60) if int(v) else (210, 55, 55)


def bit_name(v):
    return "GRN" if int(v) else "RED"


def draw_track_straight(screen, x1, x2, y, rail_gap=18, rail_thickness=5, sleeper_every=30, sleeper_len=30):
    start = min(x1, x2)
    end = max(x1, x2)

    ballast_rect = pygame.Rect(start, y - (rail_gap // 2) - 12, end - start, rail_gap + 24)
    pygame.draw.rect(screen, (215, 215, 215), ballast_rect, border_radius=12)

    y_top = y - rail_gap // 2
    y_bot = y + rail_gap // 2
    pygame.draw.line(screen, (40, 40, 40), (x1, y_top), (x2, y_top), rail_thickness)
    pygame.draw.line(screen, (40, 40, 40), (x1, y_bot), (x2, y_bot), rail_thickness)

    for sx in range(start, end, sleeper_every):
        pygame.draw.line(screen, (95, 75, 55), (sx, y - sleeper_len // 2), (sx, y + sleeper_len // 2), 4)


def draw_track_curve(screen, p1, p2, y_ctrl, rail_gap=18, rail_thickness=5):
    x1, y1 = p1
    x2, y2 = p2

    pts = []
    steps = 18
    for i in range(steps + 1):
        t = i / steps
        cx, cy = (x1 + x2) / 2, y_ctrl
        bx = (1 - t) ** 2 * x1 + 2 * (1 - t) * t * cx + t ** 2 * x2
        by = (1 - t) ** 2 * y1 + 2 * (1 - t) * t * cy + t ** 2 * y2
        pts.append((bx, by))

    pygame.draw.lines(screen, (215, 215, 215), False, pts, rail_gap + 18)

    def draw_offset_polyline(offset):
        rail_pts = []
        for i in range(len(pts)):
            if i == len(pts) - 1:
                dx = pts[i][0] - pts[i - 1][0]
                dy = pts[i][1] - pts[i - 1][1]
            else:
                dx = pts[i + 1][0] - pts[i][0]
                dy = pts[i + 1][1] - pts[i][1]
            length = math.hypot(dx, dy) or 1.0
            nx = -dy / length
            ny = dx / length
            rail_pts.append((pts[i][0] + nx * offset, pts[i][1] + ny * offset))
        pygame.draw.lines(screen, (40, 40, 40), False, rail_pts, rail_thickness)

    draw_offset_polyline(-rail_gap / 2)
    draw_offset_polyline(+rail_gap / 2)


def make_train_sprite_pro(width=130, height=46):
    s = pygame.Surface((width, height), pygame.SRCALPHA)

    shell = (18, 22, 30)
    shell2 = (30, 36, 48)
    accent = (0, 120, 215)
    window = (200, 230, 255)
    window2 = (120, 160, 200)
    metal = (95, 100, 110)
    wheel = (20, 20, 22)
    light = (255, 245, 180)

    body = pygame.Rect(10, 10, width - 20, 26)
    pygame.draw.rect(s, shell, body, border_radius=12)
    band = pygame.Rect(12, 12, width - 24, 8)
    pygame.draw.rect(s, shell2, band, border_radius=10)

    nose = pygame.Rect(width - 28, 12, 18, 22)
    pygame.draw.rect(s, shell2, nose, border_radius=10)

    stripe = pygame.Rect(16, 28, width - 40, 4)
    pygame.draw.rect(s, accent, stripe, border_radius=3)

    wx = 30
    for _ in range(5):
        pygame.draw.rect(s, window, pygame.Rect(wx, 16, 12, 10), border_radius=3)
        pygame.draw.rect(s, window2, pygame.Rect(wx, 16, 12, 10), 1, border_radius=3)
        wx += 16

    pygame.draw.polygon(s, window, [(width - 42, 16), (width - 30, 16), (width - 26, 22), (width - 42, 22)])
    pygame.draw.polygon(s, window2, [(width - 42, 16), (width - 30, 16), (width - 26, 22), (width - 42, 22)], 1)

    pygame.draw.line(s, metal, (62, 14), (62, 34), 2)
    pygame.draw.line(s, metal, (86, 14), (86, 34), 2)

    pygame.draw.rect(s, (50, 55, 65), pygame.Rect(26, 34, 28, 6), border_radius=3)
    pygame.draw.rect(s, (50, 55, 65), pygame.Rect(width - 54, 34, 28, 6), border_radius=3)

    for cx in (32, 44, width - 48, width - 36):
        pygame.draw.circle(s, wheel, (cx, 41), 5)
        pygame.draw.circle(s, metal, (cx, 41), 5, 1)

    pygame.draw.circle(s, light, (width - 12, 24), 4)
    pygame.draw.circle(s, (220, 205, 140), (width - 12, 24), 4, 1)

    pygame.draw.rect(s, (10, 10, 12), body, 2, border_radius=12)
    return s


def main():
    pygame.init()
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    pygame.display.set_caption("2D Railway OT Demo (Modbus + SCADA Overrides)")
    clock = pygame.time.Clock()

    font = pygame.font.SysFont("Arial", 18)
    big = pygame.font.SysFont("Arial", 22, bold=True)
    train_sprite_right = make_train_sprite_pro()

    total_panel_w = 3 * BLOCK_W + 2 * BLOCK_GAP
    start_x = (WIDTH - total_panel_w) // 2

    block_rects = []
    block_centers_x = []
    for i in range(3):
        rx = start_x + i * (BLOCK_W + BLOCK_GAP)
        rect = pygame.Rect(rx, PANEL_Y, BLOCK_W, PANEL_H)
        block_rects.append(rect)
        block_centers_x.append(rect.centerx)

    label_min_y = PANEL_Y + PANEL_H + LABEL_BLOCK_CLEARANCE

    track_x1 = start_x - TRACK_LEFT_PAD
    track_x2 = (start_x + total_panel_w) + TRACK_RIGHT_PAD

    junction_x = block_centers_x[1]
    siding_start = (track_x1 + 120, TRACK_Y_SIDING)

    plc = PlcClient(PLC_HOST, PLC_PORT, PLC_UNIT)
    plc.connect()

    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    last_udp = 0.0
    udp_period = 1.0 / float(UDP_SEND_HZ)
    last_plc = 0.0

    # Modes
    mode_auto = True              # local mode (M toggles)
    scada_mode_manual = False     # HR[50]=1 forces manual
    need_resync = False
    prev_comms_enabled = True
    auto_write_hold_until = 0.0

    # Signals (commanded). Start RED.
    sig_ab = RED
    sig_bc = RED
    sig_sb = RED

    turnout_to_main = True
    estop = 0

    occ = {"A": 1, "B": 0, "C": 0, "S": 1}

    # Crash animation
    crash_active = False
    crash_start = 0.0
    crash_pos = (junction_x, TRACK_Y_MAIN)
    CRASH_DURATION = 1.2

    # NEW: crash push latch
    crash_sent_to_plc = False

    # T1 priority lock
    t1_claims_b_until = 0.0

    # Auto cycle
    auto_state = AUTO_INIT_WAIT
    wait_until = time.time() + WAIT_SEC  # initial 3s pause, comms OFF

    trains = {
        "T1": {"loc": "A", "x": block_centers_x[0], "y": TRACK_Y_MAIN, "moving": False,
               "move_start": 0.0, "move_dur": MOVE_DURATION_SEC, "path": "line",
               "p0": (block_centers_x[0], TRACK_Y_MAIN), "p1": (junction_x, TRACK_Y_MAIN),
               "dir_right": True, "dest": None},
        "T2": {"loc": "S", "x": siding_start[0], "y": TRACK_Y_SIDING, "moving": False,
               "move_start": 0.0, "move_dur": MOVE_DURATION_SEC, "path": "line",
               "p0": (siding_start[0], TRACK_Y_SIDING), "p1": (junction_x, TRACK_Y_MAIN),
               "dir_right": True, "dest": None, "phase": 0},
    }

    def effective_manual():
        # NEW: if crash is active, manual is forcibly disabled
        if crash_active:
            return False
        return (not mode_auto) or scada_mode_manual

    def in_wait_window(now_t: float) -> bool:
        # NEW: if crash is active, we do NOT treat it like a wait window
        if crash_active:
            return False
        return now_t < wait_until

    def comms_enabled(now_t: float) -> bool:
        # during wait windows, we send ZERO Modbus traffic
        # NEW: crash overrides and forces comms on (to push HR103)
        if crash_active:
            return True
        return not in_wait_window(now_t)

    def set_all_signals_red():
        nonlocal sig_ab, sig_bc, sig_sb
        sig_ab = RED
        sig_bc = RED
        sig_sb = RED

    def sync_from_plc_apply():
        """Apply SCADA mode + signals + occupancy overrides from PLC."""
        nonlocal turnout_to_main, estop, sig_ab, sig_bc, sig_sb, scada_mode_manual
        if not plc.connected:
            plc.close()
            plc.connect()
            if not plc.connected:
                return False

        tm, es = plc.read_coils_basic()
        turnout_to_main = bool(tm)
        estop = int(es)

        regs_mode = plc.read_holding(HR_MODE, 1)
        if regs_mode is not None:
            scada_mode_manual = (int(regs_mode[0]) != 0)

        regs_sig = plc.read_holding(HR_SIG_AB, 3)
        if regs_sig is not None and len(regs_sig) >= 3:
            sig_ab = int(regs_sig[0]) & 1
            sig_bc = int(regs_sig[1]) & 1
            sig_sb = int(regs_sig[2]) & 1

        regs_occ = plc.read_holding(HR_OVR_OCC_BASE, 4)
        if regs_occ is not None and len(regs_occ) >= 4:
            occ["A"] = int(regs_occ[0]) & 1
            occ["B"] = int(regs_occ[1]) & 1
            occ["C"] = int(regs_occ[2]) & 1
            occ["S"] = int(regs_occ[3]) & 1

        return True

    def blit_train(x, y, facing_right=True):
        spr = train_sprite_right if facing_right else pygame.transform.flip(train_sprite_right, True, False)
        screen.blit(spr, spr.get_rect(center=(int(x), int(y))))

    def draw_block(rect, label, occupied):
        color = (255, 230, 230) if occupied else (230, 240, 255)
        pygame.draw.rect(screen, color, rect, border_radius=12)
        pygame.draw.rect(screen, (40, 40, 40), rect, 2, border_radius=12)
        screen.blit(big.render(f"Block {label}", True, (20, 20, 20)), (rect.x + 12, rect.y + 10))
        screen.blit(font.render(f"Occupied: {occupied}", True, (20, 20, 20)), (rect.x + 12, rect.y + 44))

    def draw_label_pill_right_of_head(head_x, head_y, text):
        label = font.render(text, True, (20, 20, 20))
        pad_x, pad_y = 8, 4
        rect = label.get_rect()
        bg = pygame.Rect(0, 0, rect.width + pad_x * 2, rect.height + pad_y * 2)

        bg.x = int(head_x + LABEL_RIGHT_OFFSET)
        bg.y = int(head_y + LABEL_VERTICAL_NUDGE - bg.height // 2)

        if bg.y < label_min_y:
            bg.y = label_min_y

        bg.x = clamp(bg.x, 8, WIDTH - bg.width - 8)
        bg.y = clamp(bg.y, 8, HEIGHT - bg.height - 8)

        pygame.draw.rect(screen, (250, 250, 250), bg, border_radius=8)
        pygame.draw.rect(screen, (160, 160, 160), bg, 1, border_radius=8)
        screen.blit(label, (bg.x + pad_x, bg.y + pad_y))

    def draw_signal(head_x, head_y, bit, label, mast_to_y):
        SIG_RADIUS = 14
        pygame.draw.line(screen, (30, 30, 30), (head_x, head_y + SIG_RADIUS + 2), (head_x, mast_to_y), 3)
        pygame.draw.circle(screen, bit_color(bit), (head_x, head_y), SIG_RADIUS)
        pygame.draw.circle(screen, (30, 30, 30), (head_x, head_y), SIG_RADIUS, 2)
        draw_label_pill_right_of_head(head_x, head_y, f"{label} {bit_name(bit)}")

    def start_move_line(train_key, p0, p1, dest_loc):
        tr = trains[train_key]
        tr["moving"] = True
        tr["move_start"] = time.time()
        tr["move_dur"] = MOVE_DURATION_SEC
        tr["path"] = "line"
        tr["p0"] = p0
        tr["p1"] = p1
        tr["dest"] = dest_loc
        tr["dir_right"] = (p1[0] > p0[0])

    def start_move_t2_s_to_b():
        tr = trains["T2"]
        tr["moving"] = True
        tr["move_start"] = time.time()
        tr["move_dur"] = MOVE_DURATION_SEC
        tr["path"] = "t2_s_to_b"
        tr["phase"] = 0
        tr["dest"] = "B"
        tr["dir_right"] = True

    def start_move_t2_b_to_s():
        tr = trains["T2"]
        tr["moving"] = True
        tr["move_start"] = time.time()
        tr["move_dur"] = MOVE_DURATION_SEC
        tr["path"] = "t2_b_to_s"
        tr["phase"] = 0
        tr["dest"] = "S"
        tr["dir_right"] = False

    def finish_move(train_key):
        nonlocal crash_active, crash_start, crash_pos, crash_sent_to_plc, mode_auto, scada_mode_manual
        tr = trains[train_key]
        dest = tr.get("dest")

        # collision rule: two trains in B or both arriving B
        if dest == "B":
            other = "T2" if train_key == "T1" else "T1"
            if trains[other]["loc"] == "B" or (trains[other]["moving"] and trains[other].get("dest") == "B"):
                crash_active = True
                crash_sent_to_plc = False  # arm sending HR103=1
                crash_start = time.time()
                crash_pos = (junction_x, TRACK_Y_MAIN)

                # FORCE disable manual immediately
                mode_auto = True
                scada_mode_manual = False
                set_all_signals_red()

                tr["moving"] = False
                trains[other]["moving"] = False
                tr["loc"] = "CRASH"
                trains[other]["loc"] = "CRASH"
                tr["x"], tr["y"] = crash_pos[0] - 18, crash_pos[1]
                trains[other]["x"], trains[other]["y"] = crash_pos[0] + 18, crash_pos[1]
                return

        tr["moving"] = False
        tr["loc"] = dest
        tr["dest"] = None

    def update_train_positions(now_t: float):
        arrived = []
        for k, tr in trains.items():
            if not tr["moving"]:
                continue

            elapsed = now_t - tr["move_start"]
            t = clamp(elapsed / tr["move_dur"], 0.0, 1.0)

            if tr["path"] == "line":
                x = lerp(tr["p0"][0], tr["p1"][0], t)
                y = lerp(tr["p0"][1], tr["p1"][1], t)
                tr["x"], tr["y"] = x, y

            elif tr["path"] == "t2_s_to_b":
                curve_start = (junction_x - 20, TRACK_Y_SIDING)
                curve_end = (junction_x - 5, TRACK_Y_MAIN)
                if t < 0.55:
                    tt = t / 0.55
                    x = lerp(siding_start[0], curve_start[0], tt)
                    y = TRACK_Y_SIDING
                    tr["x"], tr["y"] = x, y
                else:
                    tt = (t - 0.55) / 0.45
                    cx, cy = (curve_start[0] + curve_end[0]) / 2, (TRACK_Y_SIDING + TRACK_Y_MAIN) / 2 + 20
                    bx = (1 - tt) ** 2 * curve_start[0] + 2 * (1 - tt) * tt * cx + tt ** 2 * curve_end[0]
                    by = (1 - tt) ** 2 * curve_start[1] + 2 * (1 - tt) * tt * cy + tt ** 2 * curve_end[1]
                    tr["x"], tr["y"] = bx, by
                tr["dir_right"] = True

            elif tr["path"] == "t2_b_to_s":
                curve_start = (junction_x - 5, TRACK_Y_MAIN)
                curve_end = (junction_x - 20, TRACK_Y_SIDING)
                if t < 0.45:
                    tt = t / 0.45
                    cx, cy = (curve_start[0] + curve_end[0]) / 2, (TRACK_Y_SIDING + TRACK_Y_MAIN) / 2 + 20
                    bx = (1 - tt) ** 2 * curve_start[0] + 2 * (1 - tt) * tt * cx + tt ** 2 * curve_end[0]
                    by = (1 - tt) ** 2 * curve_start[1] + 2 * (1 - tt) * tt * cy + tt ** 2 * curve_end[1]
                    tr["x"], tr["y"] = bx, by
                else:
                    tt = (t - 0.45) / 0.55
                    x = lerp(curve_end[0], siding_start[0], tt)
                    y = TRACK_Y_SIDING
                    tr["x"], tr["y"] = x, y
                tr["dir_right"] = False

            if t >= 1.0:
                arrived.append(k)

        for k in arrived:
            if not crash_active:
                finish_move(k)

    def recompute_occ_auto():
        occ["A"] = 1 if trains["T1"]["loc"] == "A" else 0
        occ["C"] = 1 if trains["T1"]["loc"] == "C" else 0
        occ["B"] = 1 if (trains["T1"]["loc"] == "B" or trains["T2"]["loc"] == "B") else 0
        occ["S"] = 1 if trains["T2"]["loc"] == "S" else 0

    def reset_all():
        nonlocal crash_active, crash_start, sig_ab, sig_bc, sig_sb, auto_state, wait_until
        nonlocal mode_auto, scada_mode_manual, t1_claims_b_until, auto_write_hold_until, crash_sent_to_plc
        crash_active = False
        crash_sent_to_plc = False
        crash_start = 0.0
        set_all_signals_red()

        mode_auto = True
        scada_mode_manual = False
        t1_claims_b_until = 0.0
        auto_write_hold_until = 0.0

        trains["T1"].update({"loc": "A", "x": block_centers_x[0], "y": TRACK_Y_MAIN, "moving": False, "dest": None})
        trains["T2"].update({"loc": "S", "x": siding_start[0], "y": TRACK_Y_SIDING, "moving": False, "dest": None})

        recompute_occ_auto()

        auto_state = AUTO_INIT_WAIT
        wait_until = time.time() + WAIT_SEC  # comms OFF during wait

    reset_all()

    tick = 0
    last_tick = time.time()

    prev_comms_enabled = True
    need_resync = False

    while True:
        clock.tick(FPS)
        now = time.time()

        comms_on = comms_enabled(now)
        if (not prev_comms_enabled) and comms_on:
            need_resync = True
        prev_comms_enabled = comms_on


        # ---------- UDP state broadcast for 3D viewer ----------
        if (now - last_udp) >= udp_period:
            last_udp = now
            try:
                payload = {
                    "t": now,
                    "mode": "AUTO" if (not effective_manual()) else "MANUAL",
                    "comms": "ON" if comms_on else "OFF",
                    "estop": int(estop),
                    "turnout_main": 1 if turnout_to_main else 0,
                    "signals": {"ab": int(sig_ab), "bc": int(sig_bc), "sb": int(sig_sb)},
                    "occ": {"A": int(occ["A"]), "B": int(occ["B"]), "C": int(occ["C"]), "S": int(occ["S"])},
                    "trains": {
                        "T1": {
                            "x": float(trains["T1"]["x"]),
                            "y": float(trains["T1"]["y"]),
                            "dir_right": bool(trains["T1"]["dir_right"]),
                            "loc": str(trains["T1"]["loc"]),
                        },
                        "T2": {
                            "x": float(trains["T2"]["x"]),
                            "y": float(trains["T2"]["y"]),
                            "dir_right": bool(trains["T2"]["dir_right"]),
                            "loc": str(trains["T2"]["loc"]),
                        },
                    },
                    "crash": 1 if crash_active else 0,
                }
                udp_sock.sendto(json.dumps(payload).encode("utf-8"), (UDP_VIEWER_IP, UDP_VIEWER_PORT))
            except Exception:
                pass
        # ---------- CRASH: force push HR103=1 ASAP ----------
        if crash_active and (not crash_sent_to_plc):
            if not plc.connected:
                plc.close()
                plc.connect()
            if plc.connected:
                # set crash bit (do NOT depend on AUTO publishing)
                plc.write_crash_only(1)
                crash_sent_to_plc = True

        # PLC comms (ONLY when comms enabled)
        if comms_on and (now - last_plc) >= PLC_POLL_SEC:
            last_plc = now
            if not plc.connected:
                plc.close()
                plc.connect()

            # On resume, pull SCADA changes FIRST (so they can cause crash)
            if need_resync and (not crash_active):
                if sync_from_plc_apply():
                    auto_write_hold_until = time.time() + 0.5
                need_resync = False

            if plc.connected:
                tm, es = plc.read_coils_basic()
                turnout_to_main = bool(tm)
                estop = int(es)

                m = plc.read_holding(HR_MODE, 1)
                if m is not None:
                    scada_mode_manual = (int(m[0]) != 0)

                if effective_manual() and (not crash_active):
                    sync_from_plc_apply()

                # AUTO publish (but never while crash_active; crash bit is pushed separately)
                if (not effective_manual()) and (not crash_active) and time.time() >= auto_write_hold_until:
                    recompute_occ_auto()
                    plc.write_inputs(occ["A"], occ["B"], occ["C"], 0)
                    plc.write_signals(sig_ab, sig_bc, sig_sb)

                # After crash animation ends and reset_all runs, the next AUTO publish
                # will naturally keep HR103 at 0 again.

        # events
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                plc.close()
                pygame.quit()
                sys.exit()
            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    plc.close()
                    pygame.quit()
                    sys.exit()
                if event.key == pygame.K_SPACE:
                    reset_all()

                if event.key == pygame.K_m:
                    # NEW: manual toggle disabled during crash
                    if not crash_active:
                        mode_auto = not mode_auto
                        if mode_auto:
                            need_resync = True

                if event.key == pygame.K_t:
                    if comms_on and (not crash_active):
                        plc.toggle_turnout()

        if now - last_tick >= 1.0:
            tick += 1
            last_tick = now

        # crash reset
        if crash_active and (now - crash_start) >= CRASH_DURATION:
            # Before reset, try to clear crash bit on PLC quickly if possible
            if not plc.connected:
                plc.close()
                plc.connect()
            if plc.connected:
                plc.write_crash_only(0)
            reset_all()

        # -------- Movement + signalling --------
        if (not crash_active) and (not estop):
            if not effective_manual():
                if auto_state == AUTO_INIT_WAIT:
                    if now >= wait_until:
                        auto_state = AUTO_T1_A_TO_C
                        set_all_signals_red()

                elif auto_state == AUTO_T1_A_TO_C:
                    if not trains["T1"]["moving"] and trains["T1"]["loc"] == "A":
                        sig_ab, sig_bc, sig_sb = GREEN, RED, RED
                        t1_claims_b_until = time.time() + JUNCTION_HEADWAY_SEC
                        start_move_line("T1", (block_centers_x[0], TRACK_Y_MAIN), (junction_x, TRACK_Y_MAIN), "B")
                    elif (not trains["T1"]["moving"]) and trains["T1"]["loc"] == "B":
                        sig_ab, sig_bc, sig_sb = RED, GREEN, RED
                        start_move_line("T1", (junction_x, TRACK_Y_MAIN), (block_centers_x[2], TRACK_Y_MAIN), "C")
                    elif (not trains["T1"]["moving"]) and trains["T1"]["loc"] == "C":
                        set_all_signals_red()
                        auto_state = AUTO_T2_S_TO_B

                elif auto_state == AUTO_T2_S_TO_B:
                    if (not trains["T2"]["moving"]) and trains["T2"]["loc"] == "S" and turnout_to_main and now >= t1_claims_b_until:
                        sig_ab, sig_bc, sig_sb = RED, RED, GREEN
                        start_move_t2_s_to_b()
                    elif (not trains["T2"]["moving"]) and trains["T2"]["loc"] == "B":
                        set_all_signals_red()
                        auto_state = AUTO_WAIT_AT_B
                        wait_until = time.time() + WAIT_SEC

                elif auto_state == AUTO_WAIT_AT_B:
                    if now >= wait_until:
                        auto_state = AUTO_T2_B_TO_S

                elif auto_state == AUTO_T2_B_TO_S:
                    if (not trains["T2"]["moving"]) and trains["T2"]["loc"] == "B" and turnout_to_main:
                        sig_ab, sig_bc, sig_sb = RED, RED, GREEN
                        start_move_t2_b_to_s()
                    elif (not trains["T2"]["moving"]) and trains["T2"]["loc"] == "S":
                        set_all_signals_red()
                        auto_state = AUTO_T1_C_TO_A

                elif auto_state == AUTO_T1_C_TO_A:
                    if (not trains["T1"]["moving"]) and trains["T1"]["loc"] == "C":
                        sig_ab, sig_bc, sig_sb = RED, GREEN, RED
                        start_move_line("T1", (block_centers_x[2], TRACK_Y_MAIN), (junction_x, TRACK_Y_MAIN), "B")
                    elif (not trains["T1"]["moving"]) and trains["T1"]["loc"] == "B":
                        sig_ab, sig_bc, sig_sb = GREEN, RED, RED
                        start_move_line("T1", (junction_x, TRACK_Y_MAIN), (block_centers_x[0], TRACK_Y_MAIN), "A")
                    elif (not trains["T1"]["moving"]) and trains["T1"]["loc"] == "A":
                        set_all_signals_red()
                        auto_state = AUTO_WAIT_AT_A
                        wait_until = time.time() + WAIT_SEC

                elif auto_state == AUTO_WAIT_AT_A:
                    if now >= wait_until:
                        auto_state = AUTO_T1_A_TO_C

            else:
                # MANUAL: trains move only if SCADA sets signals green.
                if (not trains["T1"]["moving"]) and trains["T1"]["loc"] == "A" and sig_ab == GREEN:
                    start_move_line("T1", (block_centers_x[0], TRACK_Y_MAIN), (junction_x, TRACK_Y_MAIN), "B")
                elif (not trains["T1"]["moving"]) and trains["T1"]["loc"] == "B" and sig_bc == GREEN:
                    start_move_line("T1", (junction_x, TRACK_Y_MAIN), (block_centers_x[2], TRACK_Y_MAIN), "C")
                elif (not trains["T1"]["moving"]) and trains["T1"]["loc"] == "C" and sig_bc == GREEN:
                    start_move_line("T1", (block_centers_x[2], TRACK_Y_MAIN), (junction_x, TRACK_Y_MAIN), "B")
                elif (not trains["T1"]["moving"]) and trains["T1"]["loc"] == "B" and sig_ab == GREEN:
                    start_move_line("T1", (junction_x, TRACK_Y_MAIN), (block_centers_x[0], TRACK_Y_MAIN), "A")

                if turnout_to_main:
                    if (not trains["T2"]["moving"]) and trains["T2"]["loc"] == "S" and sig_sb == GREEN:
                        start_move_t2_s_to_b()
                    elif (not trains["T2"]["moving"]) and trains["T2"]["loc"] == "B" and sig_sb == GREEN:
                        start_move_t2_b_to_s()

            update_train_positions(now)

        # -------- Draw --------
        screen.fill((245, 245, 245))

        comms_state = "ON" if comms_on else "OFF (WAIT)"
        plc_state = "OK" if plc.connected else "DISCONNECTED"
        mode_state = "AUTO" if (not effective_manual()) else "MANUAL"
        wait_left = max(0.0, wait_until - now) if not comms_on else 0.0

        draw_text(screen, big, "Keys: M AUTO/MANUAL | T turnout | SPACE reset | ESC quit", 18, 18)
        draw_text(
            screen, font,
            f"tick={tick}  PLC={plc_state}  comms={comms_state}  mode={mode_state} (SCADA HR50={1 if scada_mode_manual else 0})  estop={estop}",
            18, 50
        )
        if not comms_on:
            draw_text(screen, font, f"WAIT: {wait_left:.1f}s (Modbus paused - SCADA can override)", 18, 72)
        else:
            draw_text(screen, font, "RUNNING", 18, 72)

        draw_text(screen, font, f"Occupancy: A={occ['A']} B={occ['B']} C={occ['C']} S={occ['S']}  crash={1 if crash_active else 0}", 18, 94)

        draw_track_straight(screen, track_x1, track_x2, TRACK_Y_MAIN,
                            rail_gap=RAIL_GAP, rail_thickness=RAIL_THICKNESS,
                            sleeper_every=SLEEPER_EVERY, sleeper_len=SLEEPER_LEN)

        draw_track_straight(screen, siding_start[0] - 40, (junction_x - 20), TRACK_Y_SIDING,
                            rail_gap=RAIL_GAP, rail_thickness=RAIL_THICKNESS,
                            sleeper_every=SLEEPER_EVERY, sleeper_len=SLEEPER_LEN)

        draw_track_curve(screen,
                         (junction_x - 20, TRACK_Y_SIDING),
                         (junction_x - 5, TRACK_Y_MAIN),
                         y_ctrl=(TRACK_Y_SIDING + TRACK_Y_MAIN) / 2 + 20,
                         rail_gap=RAIL_GAP, rail_thickness=RAIL_THICKNESS)

        turnout_color = (35, 175, 60) if turnout_to_main else (230, 185, 35)
        pygame.draw.circle(screen, turnout_color, (int(junction_x), int(TRACK_Y_MAIN - 26)), 7)
        pygame.draw.circle(screen, (30, 30, 30), (int(junction_x), int(TRACK_Y_MAIN - 26)), 7, 2)
        draw_text(screen, font, "Turnout", int(junction_x) - 30, int(TRACK_Y_MAIN - 55))

        blit_train(trains["T1"]["x"], trains["T1"]["y"], trains["T1"]["dir_right"])
        blit_train(trains["T2"]["x"], trains["T2"]["y"], trains["T2"]["dir_right"])

        draw_block(block_rects[0], "A", occ["A"])
        draw_block(block_rects[1], "B (Junction)", occ["B"])
        draw_block(block_rects[2], "C", occ["C"])

        x_ab = (block_centers_x[0] + block_centers_x[1]) // 2
        x_bc = (block_centers_x[1] + block_centers_x[2]) // 2
        lane_mid = TRACK_Y_MAIN - 70

        draw_signal(x_ab, lane_mid, sig_ab, "SigAB", mast_to_y=TRACK_Y_MAIN - 14)
        draw_signal(x_bc, lane_mid, sig_bc, "SigBC", mast_to_y=TRACK_Y_MAIN - 14)

        x_sb = junction_x - 170
        siding_lane_mid = TRACK_Y_SIDING - 70
        draw_signal(x_sb, siding_lane_mid, sig_sb, "SigSB", mast_to_y=TRACK_Y_SIDING - 14)

        if crash_active:
            crash_font = pygame.font.SysFont("Arial", 36, bold=True)
            label = crash_font.render("CRASH", True, (190, 40, 40))
            rect = label.get_rect(center=(crash_pos[0], crash_pos[1] - 120))
            screen.blit(label, rect)

        pygame.display.flip()


if __name__ == "__main__":
    main()

