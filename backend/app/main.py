"""FastAPI entry point. Each brick adds its routers here."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api import health
from app.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("rasa_incanta")


@asynccontextmanager
async def lifespan(_: FastAPI):
    for warning in get_settings().startup_warnings():
        logger.warning(warning)
    yield


app = FastAPI(title="Rasa Incanta", version=__version__, lifespan=lifespan)
app.include_router(health.router)
