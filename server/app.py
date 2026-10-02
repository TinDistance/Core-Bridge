from fastapi import FastAPI

from server.logs import hub
from server.routers import command, logs, webrtc

app = FastAPI(title="Core-Bridge Server", version="0.1.0")

app.include_router(command.router)
app.include_router(webrtc.router)
app.include_router(logs.router)


@app.on_event("startup")
async def on_startup() -> None:
    import asyncio

    hub.set_loop(asyncio.get_running_loop())


def main() -> None:
    import uvicorn

    uvicorn.run("server.app:app", host="0.0.0.0", port=8000)
