from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.fund_service import MAX_FUNDS_PER_REQUEST, FundDataError, FundService


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
service = FundService()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await service.start()
    yield
    await service.close()


app = FastAPI(
    title="Fund Lens API",
    description="基于公开持仓与实时行情的个人基金盘中估值服务",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health():
    return {"status": "ok", **await service.stats()}


@app.get("/api/funds/{code}")
async def fund_estimate(code: str):
    try:
        return await service.estimate(code)
    except FundDataError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="上游行情暂时不可用，请稍后重试") from exc


@app.get("/api/funds")
async def fund_estimates(
    codes: str = Query(..., description=f"逗号分隔的基金代码，最多 {MAX_FUNDS_PER_REQUEST} 个")
):
    code_list = list(dict.fromkeys(item.strip() for item in codes.split(",") if item.strip()))[
        :MAX_FUNDS_PER_REQUEST
    ]
    return {"funds": await service.get_estimates(code_list)}
