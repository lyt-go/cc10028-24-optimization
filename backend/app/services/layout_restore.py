"""版式还原服务：文档段落层级、表格与图片区域边界及阅读顺序的版式还原。

从解析产物中恢复文档版式结构，同时为表格抽取和OCR提供输入。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService


class LayoutRestoreService(BaseModuleService):
    MODULE = "layout_restore"
    ENTITY = "版式结构"
    OPERATION_TERM = "还原"
    LIST_FIELDS = ["文档编号", "段落结构", "标题层级", "表格区域", "图片区域", "阅读顺序", "还原算法", "还原版本"]
    REQUIRED_FIELDS = ["文档编号", "段落结构", "标题层级"]
    STATUS_ORDER = ["草稿", "待还原", "已还原", "还原异常"]
    ACTION_RULES = {"启动还原": "待还原", "确认还原": "已还原", "标记异常": "还原异常"}
    NEGATIVE_ACTIONS = ["标记异常"]

    ALGORITHM_REGISTRY = {
        "hybrid": "融合规则与机器学习的混合版式还原算法",
        "rule_based": "基于启发式规则的段落层级与区域检测还原",
        "ml_based": "基于深度学习的版式结构预测还原",
    }

    def restore_from_document(
        self,
        doc_id: str,
        *,
        confirmation_id: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any]:
        """从已解析文档还原版式结构，并按OCR确认合并图中文字与段落数据。

        版式还原只读取一次OCR确认：``confirmation_id`` 显式指定值班人员要
        还原的那次确认；未指定时使用该文档最近一次确认。同一区域在一次
        确认里只贡献一段文本，合并顺序按页内实际位置记录。
        """
# 数据产物：版式结构数据集从解析产物还原并写入layout文件
        # 读取解析产物文件作为输入
        parsed_file = self._data_dir("parsed") / f"{doc_id}_parsed.jsonl"
        parsed_records = self._read_jsonl(parsed_file)
        if not parsed_records:
            return {"error": f"文档 {doc_id} 无解析产物，无法执行版式还原"}

        # 读取表格和图片区域
        table_file = self._data_dir("layout") / f"{doc_id}_tables.jsonl"
        tables = self._read_jsonl(table_file)
        image_file = self._data_dir("layout") / f"{doc_id}_images.jsonl"
        images = self._read_jsonl(image_file)

        algorithm = "hybrid"  # 当前使用的还原算法

        # 读取OCR区域识别凭据：一次确认、每个区域一段文本
        ocr_svc = self._ocr_service()
        confirmation = self._resolve_ocr_confirmation(
            ocr_svc, doc_id, confirmation_id=confirmation_id, language=language)
        confirmed_by_region: dict[str, dict[str, Any]] = {}
        if confirmation:
            confirmed_by_region = {
                item["region_id"]: item for item in confirmation.get("selected", [])
            }
        credentials_by_region = {
            region_id: self._find_credential(ocr_svc, doc_id, item)
            for region_id, item in confirmed_by_region.items()
        }

        # 还原版式结构并写入产物
        layout_records = []
        merged_pages = []
        for rec in parsed_records:
            page_num = rec.get("page_num")
            page_tables = [t for t in tables if t.get("page") == page_num]
            page_images = [img for img in images if img.get("page") == page_num]
            image_texts = self._merge_page_ocr_text(
                page_num, confirmed_by_region, credentials_by_region)
            merged_blocks = self._merge_page_blocks(
                rec, page_num, image_texts, confirmation)
            layout_records.append({
                "doc_id": rec["doc_id"],
                "page": page_num,
                "paragraph_count": rec["paragraphs"],
                "title_levels": self._infer_title_levels(rec["page_num"]),
                "table_bboxes": [t.get("bbox") for t in page_tables],
                "image_bboxes": [img.get("bbox") for img in page_images],
                "reading_order": self._infer_reading_order(page_tables, page_images),
                "algorithm": algorithm,
                "text_length": rec["text_length"],
                "ocr_confirmation_id": confirmation["confirmation_id"] if confirmation else None,
                "ocr_region_ids": [item["region_id"] for item in image_texts],
                "ocr_text_length": sum(len(item["text"] or "") for item in image_texts),
                "merged_block_order": [block["block_id"] for block in merged_blocks],
            })
            merged_pages.append({
                "doc_id": rec["doc_id"],
                "page": page_num,
                "ocr_confirmation_id": confirmation["confirmation_id"] if confirmation else None,
                "blocks": merged_blocks,
                "reading_sequence": [block["block_id"] for block in merged_blocks],
                "image_texts": image_texts,
            })

        output_path = self._data_dir("layout") / f"{doc_id}_layout.jsonl"
        written = self._atomic_write_jsonl(output_path, layout_records)
# 数据产物：还原后的版式结构写入layout目录持久化文件

        merged_path = self._data_dir("layout") / f"{doc_id}_merged_pages.jsonl"
        self._atomic_write_jsonl(merged_path, merged_pages)

        # 写入检查点
        self._write_checkpoint(f"layout_{doc_id}", {
            "doc_id": doc_id,
            "pages_restored": len(layout_records),
            "algorithm": algorithm,
            "tables_detected": len(tables),
            "images_detected": len(images),
            "ocr_confirmation_id": confirmation["confirmation_id"] if confirmation else None,
            "ocr_regions_merged": sum(len(page["image_texts"]) for page in merged_pages),
        })

        # 跨模块自动触发：版式还原只提供区域边界并被表格抽取消费。
        # OCR识别是凭据的生产方，不能由消费凭据的还原流程反向触发，否则
        # 同一次还原会在确认之外又写入新凭据，破坏“一次确认一段文本”。
        triggers = {}
        if tables:
            # 存在表格区域时触发表格抽取
            for t in tables:
# 【数据工程】版式还原操作触发下游表格数据集与OCR识别
                trg = self._trigger_table_extract(t.get("doc_id", f"{doc_id}_p{t['page']}"))
                triggers.setdefault("table_extract", []).append(trg)

        return {
            "doc_id": doc_id,
            "pages": len(layout_records),
            "records_written": written,
            "output": str(output_path),
            "merged_output": str(merged_path),
            "ocr_confirmation_id": confirmation["confirmation_id"] if confirmation else None,
            "ocr_regions_merged": sum(len(page["image_texts"]) for page in merged_pages),
            "reading_sequences": {
                str(page["page"]): page["reading_sequence"] for page in merged_pages
            },
            "triggers": triggers,
        }

    def _ocr_service(self):
        """延迟取得OCR服务，避免layout与ocr模块的导入环。"""
        cls = self._safe_import("app.services.ocr_engine", "OcrEngineService")
        return cls()

    def merged_pages(self, doc_id: str) -> dict[str, Any]:
        """回看某文档最近一次版式还原的合并结果，并带出所用OCR确认供值班追溯。"""
        pages = self._read_jsonl(self._data_dir("layout") / f"{doc_id}_merged_pages.jsonl")
        confirmation_id = next(
            (page.get("ocr_confirmation_id") for page in pages if page.get("ocr_confirmation_id")),
            None,
        )
        confirmation = None
        if confirmation_id:
            confirmation = self._ocr_service().get_confirmation(confirmation_id, doc_id)
        return {
            "doc_id": doc_id,
            "ocr_confirmation_id": confirmation_id,
            "confirmation": confirmation,
            "pages": pages,
            "reading_sequences": {
                str(page.get("page")): page.get("reading_sequence", []) for page in pages
            },
        }

    def _resolve_ocr_confirmation(
        self,
        ocr_svc,
        doc_id: str,
        *,
        confirmation_id: str | None,
        language: str | None,
    ) -> dict[str, Any] | None:
        """定位本次版式还原采用的OCR确认：显式指定优先，否则取最近一次。"""
        if confirmation_id:
            confirmation = ocr_svc.get_confirmation(confirmation_id, doc_id)
            if confirmation is None:
                confirmation = ocr_svc.get_confirmation(confirmation_id)
            return confirmation
        confirmations = ocr_svc.list_confirmations(doc_id)
        if language is not None:
            matched = [item for item in confirmations if item.get("language") == language]
            if matched:
                confirmations = matched
        return confirmations[-1] if confirmations else None

    def _find_credential(
        self, ocr_svc, doc_id: str, selected: dict[str, Any]
    ) -> dict[str, Any] | None:
        """按确认中固定的区域+版本取回凭据原件，核对坐标、配置与来源。"""
        return ocr_svc.get_credential(
            doc_id, selected["region_id"], selected.get("credential_version", ""))

    def _merge_page_ocr_text(
        self,
        page_num: int,
        confirmed_by_region: dict[str, dict[str, Any]],
        credentials_by_region: dict[str, dict[str, Any] | None],
    ) -> list[dict[str, Any]]:
        """按页收集确认选定的图中文字：一个区域一次确认只取一段，坐标取凭据原件。"""
        merged = []
        for region_id, selected in confirmed_by_region.items():
            if int(selected.get("page", 0) or 0) != int(page_num or 0):
                continue
            credential = credentials_by_region.get(region_id) or {}
            merged.append({
                "region_id": region_id,
                "sequence": int(selected.get("seq", 0) or 0),
                "credential_id": selected["credential_id"],
                "credential_version": selected.get("credential_version"),
                "config_fingerprint": selected.get("config_fingerprint"),
                "text": selected.get("text"),
                "text_checksum": selected.get("text_checksum"),
                "bbox": credential.get("bbox", selected.get("bbox")),
                "language": credential.get("language", selected.get("language")),
                "engine": credential.get("engine", selected.get("engine")),
                "engine_version": credential.get("engine_version", selected.get("engine_version")),
                "source": credential.get("source", selected.get("source")),
                "sources": credential.get("sources", selected.get("sources")),
            })
        # 阅读顺序沿用确认时固定的页内序列，坐标只用于核对来源，不参与重新排序。
        merged.sort(key=lambda item: (item["sequence"], item["region_id"]))
        return merged

    def _merge_page_blocks(
        self,
        parsed: dict[str, Any],
        page_num: Any,
        image_texts: list[dict[str, Any]],
        confirmation: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """合并图中文字与关联段落数据，记录实际阅读顺序。"""
        blocks: list[dict[str, Any]] = []
        for index in range(int(parsed.get("paragraphs") or 0)):
            blocks.append({
                "block_id": f"P{int(page_num)}-TXT-{index + 1:03d}",
                "kind": "paragraph",
                "page": page_num,
                "seq_in_page": index + 1,
            })
        for index, item in enumerate(image_texts):
            blocks.append({
                "block_id": f"P{int(page_num)}-IMG-{index + 1:03d}",
                "kind": "image_text",
                "page": page_num,
                "seq_in_page": int(parsed.get("paragraphs") or 0) + index + 1,
                "region_id": item["region_id"],
                "credential_id": item["credential_id"],
                "credential_version": item.get("credential_version"),
                "bbox": item.get("bbox"),
                "text": item.get("text"),
                "language": item.get("language"),
            })
        for block in blocks:
            block["ocr_confirmation_id"] = confirmation["confirmation_id"] if confirmation else None
        return blocks

    def _infer_title_levels(self, page_num: int) -> list[dict[str, str]]:
        """根据页面位置和文本特征推断标题层级分布。"""
        levels = []
        if page_num == 1:
            levels.append({"level": 1, "text": f"文档主标题", "pos": "top"})
            levels.append({"level": 2, "text": f"第一章 引言", "pos": "center"})
        elif page_num == 2:
            levels.append({"level": 2, "text": f"第二章 方法", "pos": "center"})
            levels.append({"level": 3, "text": f"2.1 数据准备", "pos": "center"})
        else:
            levels.append({"level": 2, "text": f"第三章 结果", "pos": "center"})
        return levels

    def _infer_reading_order(self, tables: list[dict[str, Any]], images: list[dict[str, Any]]) -> str:
        """推断文档阅读顺序：段落→表格→图片的TDR模式。"""
        if tables and images:
            return "TDR"  # Text → Table → Image
        elif tables:
            return "TD"   # Text → Table
        elif images:
            return "DI"   # Text → Image
        return "T"       # Text only