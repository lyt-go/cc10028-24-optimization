"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services import service_for

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
# 路由配置数据集：资源路由分发管理业务模块API过滤数据
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
# 数据查询：统一资源接口按模块分派查询导出数据
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, _ = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module}.csv"'},
    )


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


# -- OCR区域识别凭据：固定路径段数与 /{module}/{entry_id} 不同，不会被通用路由抢匹配。
#    刷新区看在途/已确认，确认接口固定一次阅读顺序，回溯接口用于值班追责。
@router.get("/ocr_engine/regions/{doc_id}")
def ocr_region_status(doc_id: str) -> dict[str, Any]:
    """刷新某文档的OCR区域状态：在途请求与已确认凭据分开返回。"""
    return require_service("ocr_engine").region_status(doc_id)


@router.post("/ocr_engine/regions/{doc_id}/recognize")
def ocr_recognize_document(
    doc_id: str,
    image_area_id: str | None = Query(default=None),
    language: str | None = Query(default=None),
) -> dict[str, Any]:
    """执行识别：给定区域编号走单区识别，否则整页识别，两者共用凭据去重。"""
    service = require_service("ocr_engine")
    if image_area_id:
        return service.recognize_from_area(image_area_id, doc_id=doc_id, language=language)
    return service.batch_recognize_all_images(doc_id, language=language)


@router.get("/ocr_engine/regions/{doc_id}/versions")
def ocr_region_versions(doc_id: str) -> dict[str, Any]:
    """按区域列出所有凭据版本，用于核对换了语言配置前后产生的文本。"""
    return require_service("ocr_engine").region_versions(doc_id)


@router.post("/ocr_engine/regions/{doc_id}/confirm")
def ocr_confirm(doc_id: str, language: str | None = Query(default=None),
                page: int | None = Query(default=None)) -> dict[str, Any]:
    """生成一次OCR确认：每个区域只选定一段文本并固定阅读顺序。"""
    return require_service("ocr_engine").confirm_document(doc_id, language=language, page=page)


@router.get("/ocr_engine/confirmations/{doc_id}")
def ocr_confirmations(doc_id: str) -> list[dict[str, Any]]:
    """列出文档的全部OCR确认，值班人员据此追溯使用了哪次确认。"""
    return require_service("ocr_engine").list_confirmations(doc_id)


@router.get("/ocr_engine/confirmations/{doc_id}/{confirmation_id}")
def ocr_confirmation_detail(doc_id: str, confirmation_id: str) -> dict[str, Any]:
    """回溯单次确认：区域标识、选定文本、坐标、配置与来源都在其中。"""
    confirmation = require_service("ocr_engine").get_confirmation(confirmation_id, doc_id)
    if confirmation is None:
        raise HTTPException(status_code=404, detail=f"OCR确认 {confirmation_id} 不存在")
    return confirmation


@router.get("/layout_restore/merged/{doc_id}")
def layout_merged(doc_id: str) -> dict[str, Any]:
    """回看版式还原按页合并的阅读序列，并带出实际使用的OCR确认。"""
    return require_service("layout_restore").merged_pages(doc_id)


@router.post("/layout_restore/restore/{doc_id}")
def layout_restore_document(
    doc_id: str,
    confirmation_id: str | None = Query(default=None),
    language: str | None = Query(default=None),
) -> dict[str, Any]:
    """按指定OCR确认重新还原版式；未指定时使用最近一次确认。"""
    return require_service("layout_restore").restore_from_document(
        doc_id, confirmation_id=confirmation_id, language=language)


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult:
    service = require_service(module)
    entry, message = service.run_action(entry_id, payload.action.strip())
    return ActionResult(ok=entry is not None, message=message, entry=entry)
