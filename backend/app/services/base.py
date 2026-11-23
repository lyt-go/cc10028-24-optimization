"""模块服务基类：共享查询、登记、动作、文件IO和跨模块数据流骨架。

为文档解析与知识抽取平台18个领域服务提供统一的CRUD、持久化和
跨模块自动触发能力。各业务模块只需声明字段/状态/动作元数据，
真正的数据流在增强方法中实现。
"""
from __future__ import annotations

import json
import os
import hashlib
import threading
import time
import importlib
from pathlib import Path
from typing import Any, Iterable

from app.store import store

DATA_ROOT = Path(__file__).resolve().parent.parent.parent.parent / "data"

# 同一数据产物可能被多个并发识别请求同时读改写（例如单区识别与整页识别
# 同时命中同一个图片区域）。按文件路径持有一把可重入锁，配合临时文件
# + os.replace 的原子替换，保证刷新时永远读到完整版本。
_FILE_LOCKS: dict[str, threading.RLock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _file_lock(path: Path) -> threading.RLock:
    """取得某个产物文件对应的进程内可重入锁。"""
    key = str(path.resolve())
    with _FILE_LOCKS_GUARD:
        lock = _FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _FILE_LOCKS[key] = lock
        return lock


def _ensure_dir(path: Path) -> None:
    """创建目录（含所有父目录），已有则跳过，确保数据产物写入路径存在。"""
    path.mkdir(parents=True, exist_ok=True)


def bbox_position(bbox: str) -> tuple[float, float]:
    """从 ``(x1, y1, x2, y2)`` 形式的bbox解析上沿与左沿，用于页内阅读顺序。

    解析失败的区域排到页尾，保证同一批数据每次排序结果一致。
    """
    numbers: list[float] = []
    buff = ""
    for char in bbox:
        if char.isdigit() or char in ".-":
            buff += char
        elif buff:
            numbers.append(float(buff))
            buff = ""
    if buff:
        numbers.append(float(buff))
    if len(numbers) >= 2:
        return numbers[1], numbers[0]
    return float("inf"), float("inf")


class BaseModuleService:
    MODULE = ""
    ENTITY = ""
    LIST_FIELDS: list[str] = []
    REQUIRED_FIELDS: list[str] = []
    STATUS_ORDER: list[str] = []
    ACTION_RULES: dict[str, str] = {}
    NEGATIVE_ACTIONS: list[str] = []

    # -- 基础 CRUD --

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(self.MODULE))
        for field, expected in (filters or {}).items():
            if field in self.LIST_FIELDS:
                rows = [
                    row for row in rows
                    if expected.casefold() in str(row.get(field, "")).casefold()
                ]
        if keyword and self.LIST_FIELDS:
            field = self.LIST_FIELDS[0]
            rows = [row for row in rows if keyword.casefold() in str(row.get(field, "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(self.MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing
        rows = store.rows(self.MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in self.LIST_FIELDS})
        entry["status"] = self.STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        target = self.ACTION_RULES.get(action)
        if target is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"
        entry["status"] = target
        entry["pending"] = target != self.STATUS_ORDER[-1]
        entry["abnormal"] = action in self.NEGATIVE_ACTIONS
        return entry, f"{self.ENTITY}已{action}"

    def domain_snapshot(self) -> dict[str, Any]:
        """输出模块级数据质量快照，供跨模块消费和题位源码锚点使用。"""
        rows = store.rows(self.MODULE)
        by_status = {
            status: sum(1 for row in rows if row.get("status") == status)
            for status in self.STATUS_ORDER
# 数据产物：基础工具数据集持久化读写方法写入jsonl文件
        }
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(rows),
            "by_status": by_status,
            "abnormal": sum(1 for row in rows if row.get("abnormal")),
        }

    # -- 文件 IO 持久化工具 --

    def _data_dir(self, sub: str) -> Path:
        """获取数据子目录并确保目录存在，用于持久化解析产物、分块、实体等文件。"""
        p = DATA_ROOT / sub
        _ensure_dir(p)
        return p

    def _write_jsonl(self, filepath: Path, records: list[dict[str, Any]]) -> int:
        """向JSONL文件追加写入记录，返回写入条数。附件模式保证中断恢复不丢失已有数据。"""
        _ensure_dir(filepath.parent)
        written = 0
        with open(filepath, "a", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
                written += 1
        return written

    def _read_jsonl(self, filepath: Path) -> list[dict[str, Any]]:
        """从JSONL文件读取全部记录，文件不存在返回空列表，脏行自动跳过。"""
        if not filepath.exists():
            return []
        records = []
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return records

    def _atomic_write_jsonl(self, filepath: Path, records: Iterable[dict[str, Any]]) -> int:
        """整文件原子替换写入：先写同目录临时文件再os.replace，读者不会看到半行。"""
        _ensure_dir(filepath.parent)
        with _file_lock(filepath):
            tmp_path = filepath.with_name(
                f".{filepath.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            written = 0
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    for rec in records:
                        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
                        written += 1
                os.replace(tmp_path, filepath)
            finally:
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
            return written

    def _upsert_jsonl(
        self,
        filepath: Path,
        records: list[dict[str, Any]],
        key_fields: tuple[str, ...],
        *,
        prefer_existing: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        """按业务键合并写入JSONL产物，重复键只保留一条并保持原有先后顺序。

        单区识别与整页识别可能并发产出同一图片区域的结果，这里是唯一的
        落盘入口：读旧记录、按键覆盖、原子替换，整个过程持有文件锁。
        ``prefer_existing=True`` 时保留先到的那条（用于“先确认先定稿”）。
        """
        _ensure_dir(filepath.parent)
        with _file_lock(filepath):
            existing = self._read_jsonl(filepath)
            merged: list[dict[str, Any]] = []
            positions: dict[tuple[str, ...], int] = {}
            for rec in existing:
                key = tuple(str(rec.get(field, "")) for field in key_fields)
                positions[key] = len(merged)
                merged.append(rec)
            for rec in records:
                key = tuple(str(rec.get(field, "")) for field in key_fields)
                if key in positions:
                    if not prefer_existing:
                        merged[positions[key]] = rec
                    continue
                positions[key] = len(merged)
                merged.append(rec)
            written = self._atomic_write_jsonl(filepath, merged)
        return merged, written

    def _update_jsonl(
        self,
        filepath: Path,
        key_fields: tuple[str, ...],
        key_values: tuple[str, ...],
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        """按业务键原地更新一条记录并原子回写，返回更新后的记录（不存在返回None）。"""
        with _file_lock(filepath):
            records = self._read_jsonl(filepath)
            for rec in records:
                if tuple(str(rec.get(field, "")) for field in key_fields) == key_values:
                    rec.update(updates)
                    self._atomic_write_jsonl(filepath, records)
                    return rec
        return None

    def _write_checkpoint(self, name: str, state: dict[str, Any]) -> Path:
        """写入检查点文件，用于中断恢复与游标推进。检查点记录当前处理偏移和状态。"""
# 数据操作：存储检查点数据管理中断恢复与游标推进
        p = self._data_dir("checkpoints")
        cp_file = p / f"{name}.json"
        state["_updated_at"] = time.time()
        with open(cp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, default=str)
        return cp_file

    def _read_checkpoint(self, name: str) -> dict[str, Any] | None:
        """读取检查点文件，不存在返回None。用于中断恢复时定位上次处理进度。"""
        cp_file = self._data_dir("checkpoints") / f"{name}.json"
        if not cp_file.exists():
            return None
        with open(cp_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def _compute_checksum(self, content: str) -> str:
        """计算内容SHA256校验值（取前16位），用于文件完整性比对和版本校验。"""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    def _simulate_doc_content(self, doc_id: str, paragraphs: int = 5) -> list[str]:
        """模拟生成文档段落内容用于测试数据流，每条段落包含企业、人员、时间、地点实体。"""
        lines = []
        for i in range(paragraphs):
            lines.append(f"【第{i+1}段】本文档编号为{doc_id}，包含企业、人员、时间、地点等多类实体信息。")
            lines.append(f"  2024年第三季度，某科技集团与生态合作伙伴签订战略协议，总投资额超过5亿元。")
            lines.append(f"  首席科学家张三博士在会议上指出，知识图谱技术将重塑信息抽取的行业标准。")
            lines.append(f"  Beijing总部及Shanghai分部共500名工程师参与核心系统研发。")
        return lines

    # -- 跨模块自动触发帮助器 --

    @staticmethod
    def _safe_import(module_path: str, class_name: str):
        """延迟导入避免循环依赖，module_path如'app.services.format_parse'。"""
        mod = importlib.import_module(module_path)
        return getattr(mod, class_name)

    def _auto_create_seed(self, module: str, overrides: dict[str, Any]) -> dict[str, Any] | None:
        """跨模块自动创建配套配置条目，默认为'已生效'状态。仅追加不覆盖已有配置。"""
        rows = store.rows(module)
        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        entry = {"id": new_id}
        for field, default_val in overrides.items():
            entry[field] = default_val
        entry["status"] = "已生效"
        entry["pending"] = False
        entry["abnormal"] = False
        rows.append(entry)
        return entry

# 数据操作：存储跨模块延迟导入工具管理导入数据
    def _has_auto_triggers(self) -> bool:
        """子类覆盖为True以声明支持自动触发下游模块。"""
        return False

    # -- 本域专用跨模块触发方法 --

    def _trigger_format_parse(self, batch_id: str) -> dict[str, Any]:
        """接入批次校验通过后自动触发格式解析任务创建。"""
        try:
            cls = self._safe_import("app.services.format_parse", "FormatParseService")
            return cls().create_from_batch(batch_id)
        except Exception as e:
            return {"auto_trigger": "format_parse", "batch_id": batch_id, "error": str(e)}

    def _trigger_chunk_split(self, doc_id: str) -> dict[str, Any]:
        """解析完成后自动触发文档分块切分，按语义边界将解析产物切分为文本分块。"""
        try:
            cls = self._safe_import("app.services.chunk_split", "ChunkSplitService")
            return cls().split_from_document(doc_id)
        except Exception as e:
            return {"auto_trigger": "chunk_split", "doc_id": doc_id, "error": str(e)}

    def _trigger_entity_recognize(self, chunk_id: str) -> dict[str, Any]:
        """分块完成后自动触发命名实体识别，从分块文本中提取人名、机构、时间、地名等实体。"""
        try:
            cls = self._safe_import("app.services.entity_recognize", "EntityRecognizeService")
            return cls().recognize_from_chunk(chunk_id)
        except Exception as e:
            return {"auto_trigger": "entity_recognize", "chunk_id": chunk_id, "error": str(e)}

    def _trigger_relation_extract(self, entity_set_id: str) -> dict[str, Any]:
        """实体识别完成后自动触发关系抽取，从实体对中抽取语义关系。"""
        try:
            cls = self._safe_import("app.services.relation_extract", "RelationExtractService")
            return cls().extract_from_entities(entity_set_id)
        except Exception as e:
            return {"auto_trigger": "relation_extract", "entity_set_id": entity_set_id, "error": str(e)}

    def _trigger_kg_build(self, relation_set_id: str) -> dict[str, Any]:
        """关系抽取完成后自动触发知识图谱构建，将三元组入图。"""
        try:
            cls = self._safe_import("app.services.kg_build", "KgBuildService")
            return cls().build_from_relations(relation_set_id)
        except Exception as e:
            return {"auto_trigger": "kg_build", "relation_set_id": relation_set_id, "error": str(e)}

    def _trigger_layout_restore(self, doc_id: str) -> dict[str, Any]:
        """解析完成后自动触发版式还原，恢复段落层级和表格图片区域边界。"""
        try:
            cls = self._safe_import("app.services.layout_restore", "LayoutRestoreService")
            return cls().restore_from_document(doc_id)
        except Exception as e:
            return {"auto_trigger": "layout_restore", "doc_id": doc_id, "error": str(e)}

    def _trigger_table_extract(self, layout_id: str) -> dict[str, Any]:
        """版式还原后自动触发表格抽取，提取表格区域的行列结构化数据。"""
        try:
            cls = self._safe_import("app.services.table_extract", "TableExtractService")
            return cls().extract_from_layout(layout_id)
        except Exception as e:
            return {"auto_trigger": "table_extract", "layout_id": layout_id, "error": str(e)}

    def _trigger_ocr(self, image_area_id: str) -> dict[str, Any]:
        """版式还原后自动触发OCR识别，对图片区域进行文字识别。"""
        try:
            cls = self._safe_import("app.services.ocr_engine", "OcrEngineService")
            return cls().recognize_from_area(image_area_id)
        except Exception as e:
            return {"auto_trigger": "ocr_engine", "image_area_id": image_area_id, "error": str(e)}

    def _trigger_ocr_page(self, doc_id: str) -> dict[str, Any]:
        """版式还原后整页识别，与单区识别共用同一套区域凭据去重。"""
        try:
            cls = self._safe_import("app.services.ocr_engine", "OcrEngineService")
            return cls().batch_recognize_all_images(doc_id)
        except Exception as e:
            return {"auto_trigger": "ocr_engine", "doc_id": doc_id, "error": str(e)}

    def _trigger_vector_embed(self, chunk_id: str) -> dict[str, Any]:
        """分块完成后自动触发向量嵌入，将文本分块转换为向量索引。"""
        try:
            cls = self._safe_import("app.services.vector_embed", "VectorEmbedService")
            return cls().embed_from_chunk(chunk_id)
        except Exception as e:
            return {"auto_trigger": "vector_embed", "chunk_id": chunk_id, "error": str(e)}

    def _trigger_quality_check(self, batch_id: str) -> dict[str, Any]:
        """解析完成后自动触发抽取质量评估，对抽取结果进行抽样质检。"""
        try:
            cls = self._safe_import("app.services.extract_quality", "ExtractQualityService")
            return cls().check_from_batch(batch_id)
        except Exception as e:
            return {"auto_trigger": "extract_quality", "batch_id": batch_id, "error": str(e)}

    def _trigger_lineage_record(self, parse_task_id: str) -> dict[str, Any]:
        """解析完成后自动触发血缘链路记录，登记源文件到解析产物的全链路血缘。"""
        try:
            cls = self._safe_import("app.services.extract_lineage", "ExtractLineageService")
            return cls().record_from_parse(parse_task_id)
        except Exception as e:
            return {"auto_trigger": "extract_lineage", "parse_task_id": parse_task_id, "error": str(e)}

    def _trigger_result_deliver(self, snapshot_id: str) -> dict[str, Any]:
        """版本快照发布后自动触发结果交付，将抽取结果导出到目标系统。"""
        try:
            cls = self._safe_import("app.services.result_deliver", "ResultDeliverService")
            return cls().deliver_from_snapshot(snapshot_id)
        except Exception as e:
            return {"auto_trigger": "result_deliver", "snapshot_id": snapshot_id, "error": str(e)}