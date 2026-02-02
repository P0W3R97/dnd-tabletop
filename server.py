import asyncio
import json
import random
from dataclasses import dataclass, field
from typing import Dict, Any, Optional

import websockets


@dataclass
class Player:
    client_id: str
    name: str
    hp: int = 20


@dataclass
class GameState:
    players: Dict[str, Player] = field(default_factory=dict)
    tokens: Dict[str, Dict[str, int]] = field(default_factory=dict)  # token_id -> {"x": int, "y": int}


class TabletopServer:
    def __init__(self) -> None:
        self.clients: Dict[str, websockets.WebSocketServerProtocol] = {}  # client_id -> ws
        self.seq: int = 0
        self.state = GameState()
        self.dedup: Dict[str, int] = {}  # (client_id:event_id) -> seq

        self._shutdown = asyncio.Event()

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    async def broadcast(self, msg: Dict[str, Any]) -> None:
        data = json.dumps(msg)
        dead = []
        for cid, ws in self.clients.items():
            try:
                await ws.send(data)
            except Exception:
                dead.append(cid)
        for cid in dead:
            self.clients.pop(cid, None)

    def validate_command(self, obj: Dict[str, Any]) -> Optional[str]:
        if not isinstance(obj, dict):
            return "Message must be a JSON object."
        if obj.get("type") != "command":
            return "Expected type='command'."
        if not isinstance(obj.get("client_id"), str) or not obj["client_id"]:
            return "Missing/invalid client_id."
        if not isinstance(obj.get("event_id"), str) or not obj["event_id"]:
            return "Missing/invalid event_id."
        if not isinstance(obj.get("command"), str) or not obj["command"]:
            return "Missing/invalid command."
        if "payload" not in obj or not isinstance(obj["payload"], dict):
            return "Missing/invalid payload (must be object)."
        return None

    def make_error(self, message: str) -> Dict[str, Any]:
        return {"type": "error", "message": message}

    async def send_error_to(self, client_id: str, message: str) -> None:
        ws = self.clients.get(client_id)
        if ws is None:
            return
        try:
            await ws.send(json.dumps(self.make_error(message)))
        except Exception:
            pass

    async def handle_command(self, cmd: Dict[str, Any]) -> None:
        client_id: str = cmd["client_id"]
        event_id: str = cmd["event_id"]
        command: str = cmd["command"]
        payload: Dict[str, Any] = cmd["payload"]

        dedup_key = f"{client_id}:{event_id}"
        if dedup_key in self.dedup:
            return  # already processed

        seq = self.next_seq()

        if command == "JOIN":
            name = payload.get("name")
            if not isinstance(name, str) or not name.strip():
                await self.send_error_to(client_id, "JOIN requires payload.name as non-empty string.")
                return
            if client_id not in self.state.players:
                self.state.players[client_id] = Player(client_id=client_id, name=name.strip())
            else:
                self.state.players[client_id].name = name.strip()

            event = {
                "type": "event",
                "seq": seq,
                "event_id": event_id,
                "event_type": "JOIN",
                "payload": {"client_id": client_id, "name": self.state.players[client_id].name},
            }

        elif command == "CHAT":
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                await self.send_error_to(client_id, "CHAT requires payload.text as non-empty string.")
                return
            event = {
                "type": "event",
                "seq": seq,
                "event_id": event_id,
                "event_type": "CHAT",
                "payload": {"client_id": client_id, "text": text},
            }

        elif command == "ROLL_DICE":
            sides = payload.get("sides")
            if not isinstance(sides, int) or sides < 2 or sides > 10_000:
                await self.send_error_to(client_id, "ROLL_DICE requires payload.sides as int >=2.")
                return
            result = random.randint(1, sides)
            event = {
                "type": "event",
                "seq": seq,
                "event_id": event_id,
                "event_type": "ROLL_DICE",
                "payload": {"client_id": client_id, "sides": sides, "result": result},
            }

        elif command == "MOVE_TOKEN":
            token_id = payload.get("token_id")
            x = payload.get("x")
            y = payload.get("y")
            if not isinstance(token_id, str) or not token_id:
                await self.send_error_to(client_id, "MOVE_TOKEN requires payload.token_id as non-empty string.")
                return
            if not isinstance(x, int) or not isinstance(y, int):
                await self.send_error_to(client_id, "MOVE_TOKEN requires payload.x and payload.y as integers.")
                return
            self.state.tokens[token_id] = {"x": x, "y": y}
            event = {
                "type": "event",
                "seq": seq,
                "event_id": event_id,
                "event_type": "MOVE_TOKEN",
                "payload": {"token_id": token_id, "x": x, "y": y},
            }

        elif command == "SET_HP":
            target_id = payload.get("target_id")
            delta = payload.get("delta")
            if not isinstance(target_id, str) or not target_id:
                await self.send_error_to(client_id, "SET_HP requires payload.target_id as non-empty string.")
                return
            if not isinstance(delta, int):
                await self.send_error_to(client_id, "SET_HP requires payload.delta as integer.")
                return

            if target_id not in self.state.players:
                self.state.players[target_id] = Player(client_id=target_id, name=target_id, hp=20)

            p = self.state.players[target_id]
            p.hp = max(0, p.hp + delta)

            event = {
                "type": "event",
                "seq": seq,
                "event_id": event_id,
                "event_type": "SET_HP",
                "payload": {"target_id": target_id, "delta": delta, "new_hp": p.hp},
            }

        else:
            await self.send_error_to(client_id, f"Unknown command: {command}")
            return

        self.dedup[dedup_key] = seq
        await self.broadcast(event)

    async def handler(self, ws: websockets.WebSocketServerProtocol) -> None:
        registered_client_id: Optional[str] = None
        try:
            async for raw in ws:
                try:
                    obj = json.loads(raw)
                except Exception:
                    await ws.send(json.dumps(self.make_error("Invalid JSON.")))
                    continue

                err = self.validate_command(obj)
                if err:
                    await ws.send(json.dumps(self.make_error(err)))
                    continue

                client_id = obj["client_id"]
                registered_client_id = client_id
                self.clients[client_id] = ws

                await self.handle_command(obj)

        except websockets.ConnectionClosed:
            pass
        finally:
            if registered_client_id and self.clients.get(registered_client_id) is ws:
                self.clients.pop(registered_client_id, None)

    async def run(self, host: str, port: int) -> None:
        print(f"Server starting on ws://{host}:{port}")
        async with websockets.serve(self.handler, host, port):
            # Wait forever until Ctrl+C triggers cancellation
            await asyncio.Future()


async def main() -> None:
    server = TabletopServer()
    await server.run("0.0.0.0", 8765)


if __name__ == "__main__":
    asyncio.run(main())
