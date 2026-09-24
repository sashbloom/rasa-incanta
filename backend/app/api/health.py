"""Health check used by Railway and by anyone checking the deploy."""
import logging

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import __version__
from app.config import get_settings
from app.db import get_session

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/health")
def health(response: Response, session: Session = Depends(get_session)) -> dict:
    try:
        session.execute(text("SELECT 1"))
        database = "ok"
    except Exception:  # report, never crash the health check
        logger.exception("Database check failed")
        database = "error"
        response.status_code = 503
    return {
        "status": "ok" if database == "ok" else "degraded",
        "database": database,
        "environment": get_settings().environment,
        "version": __version__,
    }
