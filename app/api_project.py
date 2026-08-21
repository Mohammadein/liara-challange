"""
اندپوینت‌های فرم پروژه.

    GET  /api/v1/project/options   توصیف خود فرم — تا UI فیلدها را از سرور
                                   بخواند و دو جا از هم جدا نیفتند
    POST /api/v1/project/plan      پروفایل → نقشه استقرار + ذخیره در session

بعد از ثبت پروفایل، /api/chat همان session شخصی‌سازی می‌شود: مدل دیگر
نمی‌پرسد «کدام فریم‌ورک؟» چون می‌داند.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from openai import APIError
from pydantic import BaseModel, Field

from app.project import (
    DATABASES,
    DEPLOY_METHODS,
    EXPERIENCE,
    NEEDS,
    PLATFORMS,
    ProjectProfile,
    liara_json_for,
    services_for,
)
from app.observability import metrics
from app.security import llm_capacity
from app.settings import settings

log = logging.getLogger("app.project")

router = APIRouter(prefix="/api/v1/project", tags=["project"])


# ---------------------------------------------------------------- مدل‌ها

class Option(BaseModel):
    value: str
    label: str


class FormField(BaseModel):
    name: str
    label: str
    type: str                       # text | select | multiselect
    required: bool = False
    placeholder: str | None = None
    help: str | None = None
    options: list[Option] = []


class OptionsResponse(BaseModel):
    fields: list[FormField]


class PlanRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    client_id: str | None = Field(default=None, min_length=16, max_length=64)
    description: str = Field(
        min_length=3, max_length=1000,
        examples=["یه فروشگاه آنلاین با پنل ادمین و آپلود عکس محصول"],
    )
    platform: str | None = None
    database: str | None = None
    needs: list[str] = []
    deploy_method: str | None = None
    experience: str = "intermediate"


class ServiceOut(BaseModel):
    service: str
    title: str
    url: str
    why: str


class PlanResponse(BaseModel):
    request_id: str
    plan: str = Field(description="نقشه‌ی مرحله‌به‌مرحله به فارسی (Markdown)")
    services: list[ServiceOut] = Field(
        description="سرویس‌های لازم. قاعده‌محور و قطعی، نه خروجی مدل."
    )
    liara_json: dict = Field(description="فایل پیکربندی پیشنهادی")
    sources: list[dict] = []
    usage: dict = {}


# ---------------------------------------------------------------- فرم

@router.get("/options", response_model=OptionsResponse,
            summary="توصیف فیلدهای فرم پروژه")
async def options():
    """
    UI فرم را از اینجا می‌سازد، نه از روی لیست دستی خودش.

    اگر پلتفرم جدیدی به لیارا اضافه شد، فقط app/project.py عوض می‌شود و
    فرم خودکار به‌روز می‌شود — بدون اینکه فرانت و بک‌اند از هم جدا بیفتند.
    """
    def opts(d: dict[str, str]) -> list[Option]:
        return [Option(value=k, label=v) for k, v in d.items()]

    return OptionsResponse(fields=[
        FormField(
            name="description", label="چی می‌خوای بسازی؟", type="text",
            required=True,
            placeholder="مثلاً: یه فروشگاه آنلاین با پنل ادمین و آپلود عکس",
            help="هرچه دقیق‌تر بگویی، نقشه دقیق‌تر می‌شود.",
        ),
        FormField(
            name="platform", label="با چی می‌نویسی؟", type="select",
            options=opts(PLATFORMS),
            help="اگر مطمئن نیستی خالی بگذار.",
        ),
        FormField(
            name="database", label="دیتابیس", type="select",
            options=opts(DATABASES),
        ),
        FormField(
            name="needs", label="به کدام‌ها نیاز داری؟", type="multiselect",
            options=[Option(value=k, label=v["label"]) for k, v in NEEDS.items()],
        ),
        FormField(
            name="deploy_method", label="چطور دیپلوی می‌کنی؟", type="select",
            options=opts(DEPLOY_METHODS),
        ),
        FormField(
            name="experience", label="چقدر با دیپلوی آشنایی؟", type="select",
            options=opts(EXPERIENCE),
        ),
    ])


# ---------------------------------------------------------------- نقشه

@router.post("/plan", response_model=PlanResponse,
             summary="ساخت نقشه استقرار از پروفایل پروژه")
async def plan(req: PlanRequest, request: Request):
    request_id = getattr(request.state, "request_id", uuid.uuid4().hex[:12])

    if settings.use_mock:
        return JSONResponse(status_code=503, content={
            "code": "mock_mode", "request_id": request_id,
            "message": "سرور در حالت mock است."})

    profile = ProjectProfile(
        description=req.description,
        platform=req.platform,
        database=req.database,
        needs=[n for n in req.needs if n in NEEDS],
        deploy_method=req.deploy_method,
        experience=req.experience,
    )

    # سرویس‌ها قاعده‌محور تعیین می‌شوند نه با مدل: اگر کاربر گفته آپلود
    # عکس دارد، نیاز به Object Storage یک واقعیت است نه یک نظر.
    services = services_for(profile)

    from app.agent import build_plan, get_retriever  # noqa: F401
    from app.llm import LLMUnavailable
    from app.session import sessions

    acquired = await llm_capacity.acquire()
    if not acquired:
        metrics.failure("llm_capacity")
        return JSONResponse(status_code=503, content={
            "code": "service_busy", "request_id": request_id,
            "message": "سرویس موقتاً شلوغ است."})

    try:
        async with asyncio.timeout(settings.request_timeout_seconds):
            result = await build_plan(profile, services)
    except TimeoutError:
        metrics.failure("project_plan_timeout")
        return JSONResponse(status_code=504, content={
            "code": "timeout", "request_id": request_id,
            "message": "ساخت نقشه بیش از حد طول کشید."})
    except LLMUnavailable:
        metrics.failure("llm_unavailable")
        return JSONResponse(status_code=503, content={
            "code": "llm_unavailable", "request_id": request_id,
            "message": "سرویس هوش مصنوعی در دسترس نیست."})
    except APIError as exc:
        metrics.failure("llm_provider")
        log.error("llm provider error rid=%s type=%s", request_id, type(exc).__name__)
        return JSONResponse(status_code=503, content={
            "code": "llm_unavailable", "request_id": request_id,
            "message": "سرویس هوش مصنوعی در دسترس نیست."})
    except Exception:
        metrics.failure("project_plan")
        log.exception("plan failed rid=%s", request_id)
        return JSONResponse(status_code=500, content={
            "code": "internal_error", "request_id": request_id,
            "message": "خطای داخلی."})
    finally:
        llm_capacity.release()

    # پروفایل در session می‌ماند تا سؤالات بعدی شخصی‌سازی شوند
    session = sessions.get(req.session_id, req.client_id)
    session.profile = profile
    if profile.variant_hint:
        session.variant = profile.variant_hint
    session.save()

    log.info("plan rid=%s platform=%s needs=%d services=%d tokens=%d",
             request_id, profile.platform, len(profile.needs),
             len(services), result["tokens"])
    metrics.token_usage("project_plan", result["tokens"])
    metrics.observe_operation("project_plan", result["latency_ms"] / 1000)

    return PlanResponse(
        request_id=request_id,
        plan=result["plan"],
        services=[ServiceOut(**s) for s in services],
        liara_json=liara_json_for(profile),
        sources=result["sources"],
        usage={"tokens": result["tokens"], "latency_ms": result["latency_ms"]},
    )
