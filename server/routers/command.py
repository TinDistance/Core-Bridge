from fastapi import APIRouter

router = APIRouter(prefix="/command")


@router.get("/")
async def get_command() -> str:
    return "Test"
