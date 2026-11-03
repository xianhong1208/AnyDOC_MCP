"""Health check endpoints"""

from datetime import datetime

from fastapi import APIRouter

from src.auth.revocation import revocation_status

router = APIRouter(tags=["Health"], prefix="/health")


@router.get("")
async def health_check():
    """Basic health check, plus the state of MCP Gate's revocation feed.

    `revocation` reports `last_success` (last successful poll), `revoked_count` and `stale`;
    a stale feed makes the status `degraded` (the endpoint still answers 200).
    """
    revocation = revocation_status()
    return {
        "status": "degraded" if revocation.get("stale") else "healthy",
        "timestamp": datetime.now().astimezone().isoformat(),
        "revocation": revocation,
    }
