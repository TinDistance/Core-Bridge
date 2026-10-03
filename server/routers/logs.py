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
        # 历史分块发送，避免 500 行一次性阻塞 loop
        history = list(hub.history)
        for i in range(0, len(history), 50):
            for line in history[i:i + 50]:
                await websocket.send_text(line)
        while True:
            line = await queue.get()
            await websocket.send_text(line)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:
        try:
            await websocket.close()
        except Exception:
            pass
    finally:
        hub.unsubscribe(queue)
