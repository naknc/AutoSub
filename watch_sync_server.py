from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from urllib.parse import parse_qs, urlparse

logging.basicConfig(
	level=logging.INFO,
	format="%(asctime)s  %(levelname)-5s  %(message)s",
	datefmt="%H:%M:%S",
)
log = logging.getLogger("watchsync")


class RoomStore:
	def __init__(self):
		self.lock = Lock()
		self.rooms = {}

	def _room(self, code):
		return self.rooms.setdefault(
			code,
			{
				"next_id": 1,
				"events": [],
				"members": {},
				"state": None,
			},
		)

	def join(self, code, client_id, name):
		with self.lock:
			room = self._room(code)
			is_new = client_id not in room["members"]
			room["members"][client_id] = {"name": name, "last_seen": time.time()}
			self._prune(room)
			member_names = [m["name"] for m in room["members"].values()]
			if is_new:
				log.info("JOIN  room=%s  client=%s  name=%s  members=%s", code, client_id[:8], name, member_names)
			else:
				log.info("REJOIN  room=%s  client=%s  name=%s  members=%s", code, client_id[:8], name, member_names)
			return self._snapshot(room)

	def add_event(self, code, client_id, event_type, payload):
		with self.lock:
			room = self._room(code)
			room["members"].setdefault(client_id, {"name": "Guest", "last_seen": time.time()})
			room["members"][client_id]["last_seen"] = time.time()
			event = {
				"id": room["next_id"],
				"type": event_type,
				"payload": payload,
				"client_id": client_id,
				"ts": time.time(),
			}
			room["next_id"] += 1
			room["events"].append(event)
			room["events"] = room["events"][-200:]
			room["state"] = event
			self._prune(room)
			name = room["members"].get(client_id, {}).get("name", "?")
			pos_ms = payload.get("position_ms")
			pos_str = f"  pos={pos_ms}ms" if pos_ms is not None else ""
			log.info("EVENT  room=%s  id=%d  type=%-6s  from=%s(%s)%s", code, event["id"], event_type, client_id[:8], name, pos_str)
			return event["id"]

	def events_since(self, code, since, client_id):
		with self.lock:
			room = self._room(code)
			room["members"].setdefault(client_id, {"name": "Guest", "last_seen": time.time()})
			room["members"][client_id]["last_seen"] = time.time()
			self._prune(room)
			events = [e for e in room["events"] if e["id"] > since]
			result = {
				"events": events,
				"latest_event_id": room["events"][-1]["id"] if room["events"] else 0,
				"members": list(room["members"].values()),
				"state": room["state"],
			}
			if events:
				log.info("POLL  room=%s  client=%s  since=%d  delivering=%d events", code, client_id[:8], since, len(events))
			return result

	def _snapshot(self, room):
		return {
			"latest_event_id": room["events"][-1]["id"] if room["events"] else 0,
			"members": list(room["members"].values()),
			"state": room["state"],
		}

	def _prune(self, room):
		now = time.time()
		stale = [cid for cid, member in room["members"].items() if now - member["last_seen"] > 120]
		for cid in stale:
			name = room["members"][cid].get("name", "?")
			log.info("PRUNE  client=%s  name=%s  (inactive >120s)", cid[:8], name)
			room["members"].pop(cid, None)


STORE = RoomStore()


class Handler(BaseHTTPRequestHandler):
	server_version = "AutoSubWatchSync/0.1"

	def do_POST(self):
		path = self.path.rstrip("/")
		parts = path.split("/")
		if len(parts) != 4 or parts[1] != "rooms":
			log.warning("POST 404  %s", self.path)
			self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
			return
		length = int(self.headers.get("Content-Length", "0"))
		try:
			body = json.loads(self.rfile.read(length) or b"{}")
		except json.JSONDecodeError:
			log.warning("POST 400  bad JSON  %s", self.path)
			self._json({"error": "Invalid JSON"}, HTTPStatus.BAD_REQUEST)
			return
		room = parts[2]
		action = parts[3]
		client_id = body.get("client_id")
		if not client_id:
			log.warning("POST 400  missing client_id  %s", self.path)
			self._json({"error": "client_id required"}, HTTPStatus.BAD_REQUEST)
			return
		if action == "join":
			name = body.get("name") or "Guest"
			self._json(STORE.join(room, client_id, name))
			return
		if action == "events":
			event_type = body.get("type")
			if not event_type:
				log.warning("POST 400  missing type  room=%s  client=%s", room, client_id[:8])
				self._json({"error": "type required"}, HTTPStatus.BAD_REQUEST)
				return
			event_id = STORE.add_event(room, client_id, event_type, body.get("payload") or {})
			self._json({"ok": True, "event_id": event_id})
			return
		log.warning("POST 404  unknown action=%s  %s", action, self.path)
		self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

	def do_GET(self):
		url = urlparse(self.path)
		path = url.path.rstrip("/")
		parts = path.split("/")
		if len(parts) != 4 or parts[1] != "rooms" or parts[3] != "events":
			log.warning("GET 404  %s", self.path)
			self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
			return
		query = parse_qs(url.query)
		client_id = query.get("client_id", [""])[0]
		if not client_id:
			log.warning("GET 400  missing client_id  %s", self.path)
			self._json({"error": "client_id required"}, HTTPStatus.BAD_REQUEST)
			return
		since = int(query.get("since", ["0"])[0] or 0)
		self._json(STORE.events_since(parts[2], since, client_id))

	def log_message(self, fmt, *args):
		return

	def _json(self, payload, status=HTTPStatus.OK):
		body = json.dumps(payload).encode("utf-8")
		self.send_response(status)
		self.send_header("Content-Type", "application/json")
		self.send_header("Content-Length", str(len(body)))
		self.send_header("Access-Control-Allow-Origin", "*")
		self.end_headers()
		self.wfile.write(body)


if __name__ == "__main__":
	log.info("Watch sync server starting on http://0.0.0.0:8765")
	server = ThreadingHTTPServer(("0.0.0.0", 8765), Handler)
	server.serve_forever()
