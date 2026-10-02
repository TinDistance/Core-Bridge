from fastapi import APIRouter

router = APIRouter(tags=["heartbeat"])


@router.get("/test")
async def test() -> str:
    return "pong"
