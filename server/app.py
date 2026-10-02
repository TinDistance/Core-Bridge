from fastapi import FastAPI

from server.routers import command, webrtc

app = FastAPI(title="Core-Bridge Server", version="0.1.0")

app.include_router(command.router)
app.include_router(webrtc.router)


def main() -> None:
    import uvicorn

    uvicorn.run("server.app:app", host="0.0.0.0", port=8000)
