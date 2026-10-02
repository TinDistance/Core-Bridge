from fastapi import FastAPI

from server.routers import command

app = FastAPI(title="Core-Bridge Server", version="0.1.0")

app.include_router(command.router)


def main() -> None:
    import uvicorn

    uvicorn.run("server.app:app", host="0.0.0.0", port=8000)
