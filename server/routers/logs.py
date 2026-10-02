from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from server.logs import hub

router = APIRouter(tags=["logs"])


@router.delete("/logs")
async def clear_logs() -> dict:
    hub.history.clear()
    return {"ok": True}


@router.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket) -> None:
    await websocket.accept()
    queue = hub.subscribe()
    try:
        for line in list(hub.history):
            await websocket.send_text(line)
        while True:
            line = await queue.get()
            await websocket.send_text(line)
    except WebSocketDisconnect:
        pass
    finally:
        hub.unsubscribe(queue)
