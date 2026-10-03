from fastapi import APIRouter

from app.temporal.client import start_workflow

router = APIRouter()


@router.post("/start")
async def start():
    result = await start_workflow()
    return {"result": result}
