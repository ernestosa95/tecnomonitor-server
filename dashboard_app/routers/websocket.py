"""
Gestor de conexiones WebSocket y el endpoint /ws/alertas.

Nota: dashboard_app tiene su propio ConnectionManager, separado del de
server.py. Cuando dashboard_app corre montado dentro de server.py (el caso
real de producción), el /ws/alertas de server.py registra primero y gana en
el proceso fusionado -- este endpoint queda relevante sobre todo si
dashboard_app corriera standalone (modo dev, ver compiler.txt). Se extrae
tal cual estaba, sin cambiar ese comportamiento.

Segundo router extraído de dashboard.py -- ver docs/08-plan-refactor-dashboard.md.
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except:
                pass


manager = ConnectionManager()


@router.websocket("/ws/alertas")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Mantenemos el canal abierto esperando mensajes del cliente (opcional)
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
