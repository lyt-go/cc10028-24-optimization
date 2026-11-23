"""OCR识别服务：扫描图片区域的OCR引擎选择、识别语言与置信度阈值过滤配置。

单区识别（``recognize_from_area``）与整页识别（``batch_recognize_all_images``）
经常并发命中同一张图片区域。两个入口不再各写一份互不相干的文本，而是统一
落到「OCR区域识别凭据」：

- 区域标识 ``region_id`` 由文档、页码和 bbox 坐标决定，与识别入口无关；
- 凭据版本 ``credential_version`` 由语言/引擎/精度/阈值等配置指纹决定，
  配置在两次识别之间被改动时，同一区域会留下不同版本的凭据；
- 凭据按 ``(region_id, credential_version)`` 去重，重复识别只保留一条，
  并记录实际来源（单区/整页）；
- 识别请求本身也有在途/已确认状态，刷新后可区分二者；
- 版式还原只消费一次确认选定的凭据，一个区域在一次确认里只贡献一段文本。
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from app.services.base import BaseModuleService, _file_lock, bbox_position


class OcrEngineService(BaseModuleService):
    MODULE = "ocr_engine"
    ENTITY = "识别任务"
    OPERATION_TERM = "识别"
    LIST_FIELDS = ["任务编号", "图片区域", "识别语言", "识别精度", "置信度阈值", "后处理规则", "识别引擎", "引擎版本"]
    REQUIRED_FIELDS = ["任务编号", "图片区域", "识别语言"]
    STATUS_ORDER = ["草稿", "待识别", "识别中", "已完成", "识别失败"]
    ACTION_RULES = {"启动识别": "待识别", "标记完成": "已完成", "标记失败": "识别失败"}
    NEGATIVE_ACTIONS = ["标记失败"]

    ENGINE_PRESETS = {
        "paddle": {"version": "v3.2.0", "languages": ["chinese", "english", "japanese"]},
        "tesseract": {"version": "v5.3.0", "languages": ["chinese", "english", "korean"]},
        "custom": {"version": "v1.0.0", "languages": ["chinese", "english"]},
    }

    DEFAULT_ENGINE = "paddle"
    DEFAULT_THRESHOLD = 0.85
    DEFAULT_POSTPROCESS = "trim"
    SOURCE_AREA = "区域识别"
    SOURCE_PAGE = "整页识别"
    SOURCE_PRIORITY = {SOURCE_AREA: 2, SOURCE_PAGE: 1}
    STATE_IN_FLIGHT = "在途"
    STATE_CONFIRMED = "已确认"

    # -- 单区/整页识别入口 --

    def recognize_from_area(
        self,
        image_area_id: str,
        *,
        doc_id: str | None = None,
        language: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """从单个图片区域执行OCR识别，读取版式还原产物中的图片bbox并生成识别凭据。"""
        doc = doc_id or image_area_id
        images = self._load_images(doc, image_area_id)
        if not images:
            return {"error": f"图片区域文件 {image_area_id} 不存在或为空"}
        return self._recognize_images(
            doc_id=doc,
            image_area_id=image_area_id,
            images=images,
            source=self.SOURCE_AREA,
            language=language,
            config=config,
        )

    def batch_recognize_all_images(
        self,
        doc_id: str,
        *,
        language: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """整页识别某文档下所有图片区域的文字内容，与单区识别共用同一套去重凭据。"""
        layout_records = self._read_jsonl(self._data_dir("layout") / f"{doc_id}_layout.jsonl")
        if not layout_records:
            return {"error": f"文档 {doc_id} 无版式还原结果"}

        images = self._load_images(doc_id, doc_id)
        if not images:
            return {"doc_id": doc_id, "images_processed": 0}

        result = self._recognize_images(
            doc_id=doc_id,
            image_area_id=doc_id,
            images=images,
            source=self.SOURCE_PAGE,
            language=language,
            config=config,
        )
        result["doc_id"] = doc_id
        return result

    def _load_images(self, doc_id: str, image_area_id: str) -> list[dict[str, Any]]:
        """读取图片区域定义，优先精确匹配区域文件，再回退到文档级文件。"""
        images = self._read_jsonl(self._data_dir("layout") / f"{image_area_id}_images.jsonl")
        if not images and image_area_id != doc_id:
            images = self._read_jsonl(self._data_dir("layout") / f"{doc_id}_images.jsonl")
        return images

    def _recognize_images(
        self,
        *,
        doc_id: str,
        image_area_id: str,
        images: list[dict[str, Any]],
        source: str,
        language: str | None,
        config: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """对一组图片区域逐条登记识别请求并产出凭据，重复区域按版本合并。"""
        # 先按页内坐标排序并分配区域标识，使同一批区域无论来自单区文件还是
        # 整页文件、也无论文件里的原始行序如何，都得到同一套稳定标识。
        assignments: list[tuple[dict[str, Any], str, int, str]] = []
        seen_bbox: dict[str, int] = {}
        for img in images:
            bbox = str(img.get("bbox", ""))
            page = int(img.get("page", 0) or 0)
            seen_bbox[f"{page}|{bbox}"] = seen_bbox.get(f"{page}|{bbox}", -1) + 1
            repeat = seen_bbox[f"{page}|{bbox}"]
            assignments.append((img, bbox, page, self._region_id(doc_id, page, bbox, repeat)))
        assignments.sort(
            key=lambda item: (
                item[2],
                bbox_position(item[1]),
                item[3],
            )
        )

        recognized: list[dict[str, Any]] = []
        for img, bbox, page, region_id in assignments:

            resolved = self._resolve_config(img, language=language, config=config)
            request_id = self.begin_region_recognition(
                doc_id=doc_id,
                region_id=region_id,
                image_area_id=image_area_id,
                page=page,
                bbox=bbox,
                source=source,
                config=resolved,
            )
            text = self._simulate_ocr_result(bbox, resolved["language"])
            confidence = 0.92 if resolved["precision"] == "high" else 0.78
            try:
                credential = self.complete_region_recognition(
                    request_id=request_id,
                    text=text,
                    confidence=confidence,
                    config=resolved,
                )
            except BaseException:
                # 识别中途失败时把请求留在途状态并记下失败原因，刷新时仍可分辨。
                self._update_jsonl(
                    self._requests_path(doc_id), ("request_id",), (request_id,),
                    {"last_error": "识别中断", "finished_at": time.time()},
                )
                raise
            recognized.append({
                "region_id": region_id,
                "credential_id": credential["credential_id"],
                "image_area": page,
                "bbox": bbox,
                "language": resolved["language"],
                "precision": resolved["precision"],
                "recognized_text": text,
                "confidence": confidence,
                "engine": resolved["engine"],
                "engine_version": resolved["engine_version"],
                "recognized_at": credential["confirmed_at"],
            })

        return {
            "image_area_id": image_area_id,
            "doc_id": doc_id,
            "engine": self.DEFAULT_ENGINE,
            "images_processed": len(recognized),
            "records_written": len(recognized),
            "source": source,
            "recognized": recognized,
        }

    @staticmethod
    def _region_id(doc_id: str, page: int, bbox: str, repeat: int = 0) -> str:
        """由文档、页码和坐标推导区域标识，单区识别与整页识别共用同一标识。"""
        digest = _sha(bbox)
        suffix = f"-{repeat + 1}" if repeat else ""
        return f"{doc_id}-P{page:03d}-{digest[:8].upper()}{suffix}"

    def _resolve_config(
        self,
        img: dict[str, Any],
        *,
        language: str | None,
        config: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """组装本次识别配置并计算配置指纹，指纹不同即凭据版本不同。"""
        engine = self.DEFAULT_ENGINE
        resolved = {
            "engine": engine,
            "engine_version": self.ENGINE_PRESETS[engine]["version"],
            "language": language or str(img.get("language") or "chinese"),
            "precision": str(img.get("precision") or "high"),
            "threshold": self.DEFAULT_THRESHOLD,
            "postprocess": self.DEFAULT_POSTPROCESS,
        }
        for key, value in (config or {}).items():
            if value is not None:
                resolved[key] = value
        if language:
            resolved["language"] = language
        if resolved.get("engine") in self.ENGINE_PRESETS:
            resolved["engine_version"] = self.ENGINE_PRESETS[resolved["engine"]]["version"]
        fingerprint = _sha(json.dumps(resolved, ensure_ascii=False, sort_keys=True))
        resolved["config_fingerprint"] = fingerprint
        resolved["credential_version"] = f"cfg-{fingerprint[:10]}"
        return resolved

    # -- 识别请求生命周期：在途 / 已确认 --

    def begin_region_recognition(
        self,
        *,
        doc_id: str,
        region_id: str,
        image_area_id: str,
        page: int,
        bbox: str,
        source: str,
        config: dict[str, Any],
    ) -> str:
        """登记一次识别请求为在途，返回请求编号；刷新时据此区分在途与已确认。"""
        request_id = f"OCRREQ-{uuid.uuid4().hex[:12].upper()}"
        record = {
            "request_id": request_id,
            "doc_id": doc_id,
            "region_id": region_id,
            "image_area_id": image_area_id,
            "page": page,
            "bbox": bbox,
            "source": source,
            "language": config["language"],
            "engine": config["engine"],
            "engine_version": config["engine_version"],
            "threshold": config.get("threshold", self.DEFAULT_THRESHOLD),
            "postprocess": config.get("postprocess", self.DEFAULT_POSTPROCESS),
            "config_fingerprint": config["config_fingerprint"],
            "credential_version": config["credential_version"],
            "state": self.STATE_IN_FLIGHT,
            "last_error": None,
            "started_at": time.time(),
            "finished_at": None,
            "credential_id": None,
        }
        self._upsert_jsonl(
            self._requests_path(doc_id),
            [record],
            ("request_id",),
        )
        return request_id

    def complete_region_recognition(
        self,
        request_id: str,
        *,
        text: str,
        confidence: float,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """把在途请求落成已确认凭据，并按区域+配置版本去重合并。

        ``config`` 由识别入口传入完整配置；直接调用时回退到请求里登记的内容。
        """
        request = self._find_request(request_id)
        if request is None:
            raise KeyError(f"识别请求 {request_id} 不存在")

        config = {
            "engine": request["engine"],
            "engine_version": request["engine_version"],
            "language": request["language"],
            "threshold": request.get("threshold", self.DEFAULT_THRESHOLD),
            "postprocess": request.get("postprocess", self.DEFAULT_POSTPROCESS),
            "config_fingerprint": request["config_fingerprint"],
            "credential_version": request["credential_version"],
            **(config or {}),
        }
        credential_id = f"OCRCRED-{request['region_id']}-{request['credential_version']}"
        credential = {
            "credential_id": credential_id,
            "doc_id": request["doc_id"],
            "region_id": request["region_id"],
            "page": request["page"],
            "bbox": request["bbox"],
            "image_area_id": request["image_area_id"],
            "source": request["source"],
            "sources": [request["source"]],
            "request_ids": [request_id],
            "language": config["language"],
            "engine": config["engine"],
            "engine_version": config["engine_version"],
            "threshold": config["threshold"],
            "postprocess": config["postprocess"],
            "config_fingerprint": config["config_fingerprint"],
            "credential_version": config["credential_version"],
            "text": text,
            "text_checksum": _sha(text),
            "confidence": round(float(confidence), 4),
            "state": self.STATE_CONFIRMED,
            "confirmed_at": time.time(),
        }
        stored = self._store_credential(credential)
        self._update_jsonl(
            self._requests_path(request["doc_id"]),
            ("request_id",),
            (request_id,),
            {
                "state": self.STATE_CONFIRMED,
                "finished_at": time.time(),
                "credential_id": stored["credential_id"],
            },
        )
        return stored

    def _find_request(self, request_id: str) -> dict[str, Any] | None:
        for path in self._data_dir("ocr").glob("*_ocr_requests.jsonl"):
            for record in self._read_jsonl(path):
                if record.get("request_id") == request_id:
                    return record
        return None

    def _store_credential(self, credential: dict[str, Any]) -> dict[str, Any]:
        """按 (region_id, credential_version) 去重写入凭据，来源合并保持确定性。"""
        path = self._credentials_path(credential["doc_id"])
        with _file_lock(path):
            records = self._read_jsonl(path)
            for index, existing in enumerate(records):
                if (
                    existing.get("region_id") == credential["region_id"]
                    and existing.get("credential_version") == credential["credential_version"]
                ):
                    records[index] = self._merge_credentials(existing, credential)
                    self._atomic_write_jsonl(path, records)
                    return records[index]
            records.append(credential)
            self._atomic_write_jsonl(path, records)
            return credential

    def _merge_credentials(
        self, existing: dict[str, Any], incoming: dict[str, Any]
    ) -> dict[str, Any]:
        """合并同一区域同一配置版本的重复凭据，结果与到达先后无关。"""
        sources = sorted(set(existing.get("sources", [])) | set(incoming.get("sources", [])))
        request_ids = sorted(set(existing.get("request_ids", [])) | set(incoming.get("request_ids", [])))
        candidates = [existing, incoming]
        if existing.get("text_checksum") != incoming.get("text_checksum"):
            # 同配置但文本不同属于异常冲突：按置信度和文本校验值确定唯一结果。
            # 候选集本身对称排序，保证合并结果与到达先后无关。
            candidates.sort(
                key=lambda item: (float(item.get("confidence", 0)), str(item.get("text_checksum", ""))),
                reverse=True,
            )
        winner = candidates[0]
        merged = dict(winner)
        merged["sources"] = sources
        merged["source"] = max(sources, key=lambda name: self.SOURCE_PRIORITY.get(name, 0))
        merged["request_ids"] = request_ids
        merged["confidence"] = round(max(float(item.get("confidence", 0)) for item in candidates), 4)
        merged["confirmed_at"] = max(
            float(item.get("confirmed_at") or 0) for item in candidates)
        return merged

    # -- 刷新查询：在途与已确认 --

    def region_status(self, doc_id: str) -> dict[str, Any]:
        """刷新区域识别状态：同时返回在途请求和所有已确认凭据版本。"""
        requests = self._read_jsonl(self._requests_path(doc_id))
        credentials = self._read_jsonl(self._credentials_path(doc_id))
        in_flight = [
            {
                "request_id": rec.get("request_id"),
                "region_id": rec.get("region_id"),
                "page": rec.get("page"),
                "bbox": rec.get("bbox"),
                "source": rec.get("source"),
                "language": rec.get("language"),
                "credential_version": rec.get("credential_version"),
                "state": self.STATE_IN_FLIGHT,
                "started_at": rec.get("started_at"),
            }
            for rec in requests
            if rec.get("state") == self.STATE_IN_FLIGHT
        ]
        confirmed = sorted(
            (
                {
                    "credential_id": rec.get("credential_id"),
                    "region_id": rec.get("region_id"),
                    "page": rec.get("page"),
                    "bbox": rec.get("bbox"),
                    "language": rec.get("language"),
                    "engine": rec.get("engine"),
                    "engine_version": rec.get("engine_version"),
                    "threshold": rec.get("threshold"),
                    "postprocess": rec.get("postprocess"),
                    "credential_version": rec.get("credential_version"),
                    "source": rec.get("source"),
                    "sources": rec.get("sources"),
                    "confidence": rec.get("confidence"),
                    "text": rec.get("text"),
                    "text_checksum": rec.get("text_checksum"),
                    "state": self.STATE_CONFIRMED,
                }
                for rec in credentials
            ),
            key=self._region_order_key,
        )
        confirmations = self.list_confirmations(doc_id)
        return {
            "doc_id": doc_id,
            "in_flight": in_flight,
            "confirmed": confirmed,
            "latest_confirmation_id": confirmations[-1]["confirmation_id"] if confirmations else None,
            "confirmation_count": len(confirmations),
            "in_flight_count": len(in_flight),
            "confirmed_count": len(confirmed),
            "refreshed_at": time.time(),
        }

    def region_versions(self, doc_id: str) -> dict[str, list[dict[str, Any]]]:
        """按区域归集所有凭据版本，便于核对同一区域在不同配置下产生的文本。"""
        versions: dict[str, list[dict[str, Any]]] = {}
        for rec in self._read_jsonl(self._credentials_path(doc_id)):
            versions.setdefault(rec.get("region_id", ""), []).append(rec)
        return versions

    # -- 一次确认：每个区域只选定一段文本 --

    def confirm_document(
        self,
        doc_id: str,
        *,
        language: str | None = None,
        page: int | None = None,
    ) -> dict[str, Any]:
        """为文档生成一次OCR确认，每个区域只选定一个凭据版本并固定阅读顺序。"""
        credentials = [
            rec for rec in self._read_jsonl(self._credentials_path(doc_id))
            if page is None or int(rec.get("page", 0) or 0) == page
        ]
        if not credentials:
            return {"error": f"文档 {doc_id} 尚无已确认的OCR区域凭据", "doc_id": doc_id}

        selected: dict[str, dict[str, Any]] = {}
        for rec in credentials:
            current = selected.get(rec["region_id"])
            if current is None or self._selection_key(rec, language) < self._selection_key(current, language):
                selected[rec["region_id"]] = rec

        # 区域在确认里的先后由页内位置决定，与凭据写入顺序无关。
        ordered = sorted(selected.values(), key=self._region_order_key)
        page_seq: dict[int, int] = {}
        selected_records = []
        for rec in ordered:
            region_page = int(rec.get("page", 0) or 0)
            page_seq[region_page] = page_seq.get(region_page, 0) + 1
            selected_records.append({
                "seq": page_seq[region_page],
                "region_id": rec["region_id"],
                "credential_id": rec["credential_id"],
                "page": rec.get("page"),
                "bbox": rec.get("bbox"),
                "text": rec.get("text"),
                "text_checksum": rec.get("text_checksum"),
                "language": rec.get("language"),
                "engine": rec.get("engine"),
                "engine_version": rec.get("engine_version"),
                "threshold": rec.get("threshold"),
                "postprocess": rec.get("postprocess"),
                "credential_version": rec.get("credential_version"),
                "config_fingerprint": rec.get("config_fingerprint"),
                "source": rec.get("source"),
                "sources": rec.get("sources"),
            })
        payload = {
            "doc_id": doc_id,
            "language": language,
            "page": page,
            "selected": [[item["region_id"], item["credential_id"]] for item in selected_records],
        }
        confirmation_id = "CONF-" + _sha(json.dumps(payload, ensure_ascii=False, sort_keys=True))[:12].upper()
        confirmation = {
            "confirmation_id": confirmation_id,
            "doc_id": doc_id,
            "page": page,
            "language": language,
            "status": self.STATE_CONFIRMED,
            "selected_count": len(selected_records),
            "selected": selected_records,
            "reading_sequence": [item["region_id"] for item in selected_records],
            "created_at": time.time(),
        }
        self._upsert_jsonl(
            self._confirmations_path(doc_id),
            [confirmation],
            ("confirmation_id",),
            prefer_existing=True,
        )
        # 幂等确认重复调用时，返回的应是已落盘的那份（含首次生成时间）。
        return self.get_confirmation(confirmation_id, doc_id) or confirmation

    def _selection_key(self, credential: dict[str, Any], language: str | None) -> tuple:
        """确定性的凭据选择顺序：语言匹配优先，其次来源优先级、置信度、版本号。"""
        language_rank = 0 if language is None or credential.get("language") == language else 1
        source_rank = -self.SOURCE_PRIORITY.get(str(credential.get("source")), 0)
        confidence = -float(credential.get("confidence") or 0)
        return (
            language_rank,
            source_rank,
            confidence,
            str(credential.get("credential_version", "")),
            str(credential.get("credential_id", "")),
        )

    @staticmethod
    def _region_order_key(record: dict[str, Any]) -> tuple:
        """区域在页内的实际顺序：先页码，再按 bbox 上沿、左沿，最后区域标识。"""
        top, left = bbox_position(str(record.get("bbox", "")))
        return (
            int(record.get("page", 0) or 0),
            top,
            left,
            str(record.get("region_id", "")),
            str(record.get("credential_version", "")),
        )

    def list_confirmations(self, doc_id: str) -> list[dict[str, Any]]:
        """列出文档的全部OCR确认，值班人员可据此追溯版式还原使用了哪一次。"""
        rows = self._read_jsonl(self._confirmations_path(doc_id))
        # 同一毫秒内生成的确认也要稳定排序，否则“最近一次”会随读取顺序漂移。
        return sorted(
            rows,
            key=lambda rec: (
                float(rec.get("created_at") or 0),
                rec.get("confirmation_id", ""),
            ),
        )

    def get_confirmation(self, confirmation_id: str, doc_id: str | None = None) -> dict[str, Any] | None:
        """按确认编号回溯确认内容；未给文档编号时在全部确认文件中查找。"""
        paths = (
            [self._confirmations_path(doc_id)]
            if doc_id
            else sorted(self._data_dir("ocr").glob("*_ocr_confirmations.jsonl"))
        )
        for path in paths:
            for rec in self._read_jsonl(path):
                if rec.get("confirmation_id") == confirmation_id:
                    return rec
        return None

    def get_credential(
        self, doc_id: str, region_id: str, credential_version: str
    ) -> dict[str, Any] | None:
        """按区域和凭据版本取回凭据原件，供版式还原核对坐标与配置来源。"""
        for rec in self._read_jsonl(self._credentials_path(doc_id)):
            if (
                rec.get("region_id") == region_id
                and rec.get("credential_version") == credential_version
            ):
                return rec
        return None

    # -- 产物路径与模拟识别 --

    def _requests_path(self, doc_id: str):
        return self._data_dir("ocr") / f"{doc_id}_ocr_requests.jsonl"

    def _credentials_path(self, doc_id: str):
        return self._data_dir("ocr") / f"{doc_id}_ocr_credentials.jsonl"

    def _confirmations_path(self, doc_id: str):
        return self._data_dir("ocr") / f"{doc_id}_ocr_confirmations.jsonl"

    def _simulate_ocr_result(self, bbox: str, language: str) -> str:
        """模拟OCR引擎识别结果，生成对应语言的文本内容。"""
        if language == "chinese":
            return "根据OCR引擎识别，该区域包含企业名称、签署日期与合同条款等关键信息。"
        elif language == "english":
            return "According to OCR engine recognition, this region contains company name, signing date, and contract clause."
        elif language == "japanese":
            return (
                "OCRエンジンの認識によると、この領域には会社名、"
                "署名日、契約条項などの重要情報が含まれています。"
            )
        return f"OCR result for region {bbox}"


def _sha(value: str) -> str:
    """对文本计算SHA256，用于区域标识、凭据版本与文本校验。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
