from fastapi import WebSocket

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, WebSocket] = {}

    async def connect(self, sender: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[sender] = websocket

    def disconnect(self, sender: str):
        if sender in self.active_connections:
            del self.active_connections[sender]

    async def send(self, sender: str, message: str):
        ws = self.active_connections.get(sender)
        if ws:
            await ws.send_json({"reply": message})

manager = ConnectionManager()