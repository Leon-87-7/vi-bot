import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    logger.info("Video Intelligence Bot starting")
    yield
    logger.info("Video Intelligence Bot shutting down")


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}
