"""
Flappy Bird Master Server
=========================
Handles all rooms in one process.

Admin terminal commands (type while running):
  rooms                          - list all rooms
  players                        - list all connected players
  notify <msg>                   - broadcast notification to ALL players
  notify-room <code> <msg>       - notify everyone in a specific room
  kick <player_id>               - disconnect a player
  close <room_code>              - close a room and kick everyone in it
  help                           - show this list
"""

import asyncio
import websockets
import json
import random
import string
import time
import threading
import sys

# ─────────────────────────────────────────
# GLOBAL STATE
# ─────────────────────────────────────────
rooms   = {}   # code -> Room
players = {}   # ws   -> Player
next_pid = 0
loop_ref = None   # set once asyncio loop starts

class Room:
    def __init__(self, code, name, owner_id, settings):
        self.code      = code
        self.name      = name
        self.owner_id  = owner_id
        self.seed      = random.randint(0, 999999)
        self.created   = time.time()
        self.members   = set()   # set of ws
        self.settings  = settings  # dict of tweakable values
        self.official  = settings.get("official", False)

    def to_dict(self):
        return {
            "code":     self.code,
            "name":     self.name,
            "players":  len(self.members),
            "mode":     self.settings.get("mode", "endless"),
            "official": self.official,
            "settings": self.settings,
        }

class Player:
    def __init__(self, pid, ws):
        self.pid      = pid
        self.ws       = ws
        self.username = f"Player{pid+1}"
        self.room     = None   # Room or None
        self.y        = 350
        self.vel      = 0
        self.score    = 0
        self.dead     = False

    def to_dict(self):
        return {
            "id":       self.pid,
            "username": self.username,
            "score":    self.score,
            "dead":     self.dead,
            "y":        self.y,
            "vel":      self.vel,
        }

DEFAULT_SETTINGS = {
    "mode":         "endless",    # endless | timeattack | race
    "time_limit":   60,           # seconds (timeattack)
    "race_score":   10,           # score to win (race)
    "gravity":      0.5,
    "jump_force":   -9,
    "pipe_speed":   3,
    "pipe_gap":     0.25,
    "pipe_width":   0.15,
    "bird_size":    0.05,
    "max_players":  10,
    "official":     False,
}

OFFICIAL_ROOMS = [
    {"name": "Official #1", "settings": {**DEFAULT_SETTINGS, "official": True}},
    {"name": "Official #2", "settings": {**DEFAULT_SETTINGS, "official": True}},
    {"name": "Official #3 — Fast", "settings": {**DEFAULT_SETTINGS, "official": True, "pipe_speed": 5, "pipe_gap": 0.2}},
]

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────
def make_code():
    while True:
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
        if code not in rooms:
            return code

async def send(ws, data):
    try:
        await ws.send(json.dumps(data))
        print("""    Sent to {}: {}""".format(ws, data))
    except:
        pass

async def broadcast_room(room, data, exclude=None):
    msg = json.dumps(data)
    for ws in list(room.members):
        if ws != exclude:
            try:
                await ws.send(msg)
            except:
                pass

async def broadcast_all(data):
    msg = json.dumps(data)
    for ws in list(players.keys()):
        try:
            await ws.send(msg)
        except:
            pass

async def send_room_list(ws):
    await send(ws, {
        "type":  "room_list",
        "rooms": [r.to_dict() for r in rooms.values()],
    })

async def remove_player_from_room(player):
    room = player.room
    if not room:
        return
    room.members.discard(player.ws)
    player.room = None
    await broadcast_room(room, {"type": "left", "id": player.pid})
    # Notify lobby watchers
    await broadcast_all({"type": "room_update", "room": room.to_dict()})
    # Clean up empty non-official rooms
    if len(room.members) == 0 and not room.official:
        del rooms[room.code]
        await broadcast_all({"type": "room_removed", "code": room.code})

# ─────────────────────────────────────────
# HANDLER
# ─────────────────────────────────────────
async def handler(ws):
    global next_pid
    pid    = next_pid
    next_pid += 1
    player = Player(pid, ws)
    players[ws] = player
    print(f"[+] Player {pid} connected. Total: {len(players)}")

    await send(ws, {"type": "welcome", "id": pid})
    await send_room_list(ws)

    try:
        async for message in ws:
            data = json.loads(message)
            t    = data.get("type")
            p    = players[ws]

            # ── set username ──
            if t == "set_username":
                p.username = data.get("username","").strip()[:16] or p.username
                if p.room:
                    await broadcast_room(p.room, {
                        "type": "username_update", "id": p.pid, "username": p.username
                    }, exclude=ws)
                print(f"    Player {pid} username: {p.username}")

            # ── get room list ──
            elif t == "get_rooms":
                await send_room_list(ws)

            # ── create room ──
            elif t == "create_room":
                if p.room:
                    await remove_player_from_room(p)
                s    = {**DEFAULT_SETTINGS, **{
                    k: data.get(k, DEFAULT_SETTINGS[k])
                    for k in DEFAULT_SETTINGS
                    if k != "official"
                }}
                s["official"] = False
                code = make_code()
                room = Room(code, data.get("name","My Room")[:30], pid, s)
                rooms[code] = room
                room.members.add(ws)
                p.room = room
                await send(ws, {
                    "type": "joined_room",
                    "room": room.to_dict(),
                    "seed": room.seed,
                    "your_id": pid,
                    "roster": [],
                })
                await broadcast_all({"type": "room_added", "room": room.to_dict()})
                print(f"    Room created: {code} '{room.name}' by {p.username}")

            # ── join room ──
            elif t == "join_room":
                code = data.get("code","").upper().strip()
                if code not in rooms:
                    await send(ws, {"type": "error", "msg": "Room not found"})
                    continue
                room = rooms[code]
                if len(room.members) >= room.settings.get("max_players", 10):
                    await send(ws, {"type": "error", "msg": "Room is full"})
                    continue
                if p.room:
                    await remove_player_from_room(p)
                room.members.add(ws)
                p.room = room
                roster = [players[m].to_dict() for m in room.members if m != ws and m in players]
                await send(ws, {
                    "type":   "joined_room",
                    "room":   room.to_dict(),
                    "seed":   room.seed,
                    "your_id": pid,
                    "roster": roster,
                })
                await broadcast_room(room, {
                    "type": "player_joined",
                    "id":   pid,
                    "username": p.username,
                }, exclude=ws)
                await broadcast_all({"type": "room_update", "room": room.to_dict()})
                print(f"    {p.username} joined room {code}")

            # ── leave room ──
            elif t == "leave_room":
                await remove_player_from_room(p)

            # ── game state ──
            elif t == "state":
                p.y, p.vel, p.score, p.dead = (
                    data.get("y", p.y), data.get("vel", p.vel),
                    data.get("score", p.score), data.get("dead", p.dead)
                )
                if p.room:
                    await broadcast_room(p.room, {
                        "type": "state", "id": pid,
                        "username": p.username,
                        "y": p.y, "vel": p.vel,
                        "score": p.score, "dead": p.dead,
                    }, exclude=ws)

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        await remove_player_from_room(player)
        del players[ws]
        print(f"[-] Player {pid} ({player.username}) disconnected. Total: {len(players)}")

# ─────────────────────────────────────────
# OFFICIAL ROOMS SETUP
# ─────────────────────────────────────────
def create_official_rooms():
    for cfg in OFFICIAL_ROOMS:
        code = make_code()
        room = Room(code, cfg["name"], -1, cfg["settings"])
        rooms[code] = room
        print(f"  Official room: [{code}] {cfg['name']}")

# ─────────────────────────────────────────
# ADMIN TERMINAL
# ─────────────────────────────────────────
def admin_loop():
    """Runs in a background thread, reads stdin commands."""
    print("\nAdmin terminal ready. Type 'help' for commands.\n")
    while True:
        try:
            cmd = input().strip()
        except EOFError:
            break
        if not cmd:
            continue

        parts = cmd.split(" ", 2)
        op    = parts[0].lower()

        if op == "help":
            print(__doc__)

        elif op == "rooms":
            if not rooms:
                print("  No rooms.")
            for r in rooms.values():
                print(f"  [{r.code}] {r.name!r:30s} players={len(r.members)} mode={r.settings['mode']} official={r.official}")

        elif op == "players":
            if not players:
                print("  No players.")
            for p in players.values():
                room_code = p.room.code if p.room else "lobby"
                print(f"  id={p.pid} name={p.username!r} room={room_code} score={p.score}")

        elif op == "notify":
            msg = cmd[7:].strip()
            if not msg:
                print("Usage: notify <message>"); continue
            asyncio.run_coroutine_threadsafe(
                broadcast_all({"type": "notification", "msg": msg, "level": "info"}),
                loop_ref
            )
            print(f"  Notified all: {msg}")

        elif op == "notify-room":
            if len(parts) < 3:
                print("Usage: notify-room <code> <message>"); continue
            code, msg = parts[1].upper(), parts[2]
            if code not in rooms:
                print(f"  Room {code} not found"); continue
            asyncio.run_coroutine_threadsafe(
                broadcast_room(rooms[code], {"type": "notification", "msg": msg, "level": "info"}),
                loop_ref
            )
            print(f"  Notified room {code}: {msg}")

        elif op == "kick":
            if len(parts) < 2:
                print("Usage: kick <player_id>"); continue
            try:
                target_id = int(parts[1])
            except ValueError:
                print("  Invalid player id"); continue
            target_ws = next((ws for ws, p in players.items() if p.pid == target_id), None)
            if not target_ws:
                print(f"  Player {target_id} not found"); continue
            asyncio.run_coroutine_threadsafe(target_ws.close(), loop_ref)
            print(f"  Kicked player {target_id}")

        elif op == "close":
            if len(parts) < 2:
                print("Usage: close <room_code>"); continue
            code = parts[1].upper()
            if code not in rooms:
                print(f"  Room {code} not found"); continue
            room = rooms[code]
            asyncio.run_coroutine_threadsafe(
                broadcast_room(room, {"type": "notification", "msg": "This room has been closed by admin.", "level": "warn"}),
                loop_ref
            )
            for ws in list(room.members):
                asyncio.run_coroutine_threadsafe(ws.close(), loop_ref)
            print(f"  Closed room {code}")

        else:
            print(f"  Unknown command: {op}. Type 'help'.")

# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
async def main():
    global loop_ref
    loop_ref = asyncio.get_event_loop()
    create_official_rooms()
    threading.Thread(target=admin_loop, daemon=True).start()
    print(f"\nFlappy Bird Master Server running on ws://0.0.0.0:8765\n")
    async with websockets.serve(handler, "0.0.0.0", 8765):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())