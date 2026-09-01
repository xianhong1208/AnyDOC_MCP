"""Health check endpoints"""

from datetime import datetime

from fastapi import APIRouter

router = APIRouter(tags=["Health"], prefix="/health")


@router.get("")
async def health_check():
    """Basic health check"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().astimezone().isoformat()
    }
