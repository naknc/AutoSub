from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

logging.basicConfig(
	level=logging.INFO,
	format="%(asctime)s  %(levelname)-5s  %(message)s",
	datefmt="%H:%M:%S",
)
log = logging.getLogger("watchsync")

# {room_code: Room}
rooms: dict[str, Room] = {}


class Room:
	def __init__(self, code: str):
		self.code = code
		# {client_id: {"name": str, "ws": websocket}}
		self.clients: dict[str, dict] = {}
		self.state: dict | None = None

	def add(self, client_id: str, name: str, ws):
		is_new = client_id not in self.clients
		self.clients[client_id] = {"name": name, "ws": ws}
		action = "JOIN" if is_new else "REJOIN"
		log.info("%s  room=%s  client=%s  name=%s  members=%s", action, self.code, client_id[:8], name, self.member_names())
		return is_new

	def remove(self, client_id: str):
		info = self.clients.pop(client_id, None)
		if info:
			log.info("LEAVE  room=%s  client=%s  name=%s  members=%s", self.code, client_id[:8], info["name"], self.member_names())

	def member_names(self) -> list[str]:
		return [c["name"] for c in self.clients.values()]

	def member_list(self) -> list[dict]:
		return [{"name": c["name"]} for c in self.clients.values()]

	async def broadcast(self, message: str, exclude_id: str | None = None):
		targets = [
			c["ws"] for cid, c in self.clients.items()
			if cid != exclude_id
		]
		if targets:
			await asyncio.gather(
				*(ws.send(message) for ws in targets),
				return_exceptions=True,
			)


def get_room(code: str) -> Room:
	if code not in rooms:
		rooms[code] = Room(code)
	return rooms[code]


async def handler(ws):
	client_id: str | None = None
	room: Room | None = None

	try:
		async for raw in ws:
			try:
				msg = json.loads(raw)
			except json.JSONDecodeError:
				await ws.send(json.dumps({"error": "invalid JSON"}))
				continue

			action = msg.get("action")

			if action == "join":
				client_id = msg.get("client_id")
				name = msg.get("name", "Guest")
				room_code = msg.get("room", "").strip()
				if not client_id or not room_code:
					await ws.send(json.dumps({"error": "client_id and room required"}))
					continue

				# leave previous room if switching
				if room and room.code != room_code:
					room.remove(client_id)
					if not room.clients:
						rooms.pop(room.code, None)

				room = get_room(room_code)
				room.add(client_id, name, ws)

				await ws.send(json.dumps({
					"action": "joined",
					"members": room.member_list(),
					"state": room.state,
				}))

				# tell everyone else about the new member
				await room.broadcast(json.dumps({
					"action": "members",
					"members": room.member_list(),
				}), exclude_id=client_id)

			elif action == "event":
				if not room or not client_id:
					await ws.send(json.dumps({"error": "join a room first"}))
					continue

				event_type = msg.get("type")
				payload = msg.get("payload", {})
				if not event_type:
					continue

				pos_ms = payload.get("position_ms")
				pos_str = f"  pos={pos_ms}ms" if pos_ms is not None else ""
				name = room.clients.get(client_id, {}).get("name", "?")
				log.info("EVENT  room=%s  type=%-6s  from=%s(%s)%s", room.code, event_type, client_id[:8], name, pos_str)

				event = {
					"action": "event",
					"type": event_type,
					"payload": payload,
					"client_id": client_id,
					"ts": time.time(),
				}
				room.state = event

				await room.broadcast(json.dumps(event), exclude_id=client_id)

			elif action == "ping":
				await ws.send(json.dumps({"action": "pong"}))

	except websockets.ConnectionClosed:
		pass
	finally:
		if room and client_id:
			room.remove(client_id)
			if room.clients:
				try:
					await room.broadcast(json.dumps({
						"action": "members",
						"members": room.member_list(),
					}))
				except Exception:
					pass
			else:
				rooms.pop(room.code, None)


async def main():
	log.info("Watch sync server starting on ws://0.0.0.0:8765")
	async with websockets.serve(handler, "0.0.0.0", 8765):
		await asyncio.Future()


if __name__ == "__main__":
	asyncio.run(main())
