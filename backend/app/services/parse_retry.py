"""失败重采服务：解析、识别与抽取失败环节的失败原因归档与按范围重采恢复。

从解析失败任务中创建重采任务，按失败环节恢复，支持断点续采。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ParseRetryService(BaseModuleService):
    MODULE = "parse_retry"
    ENTITY = "重采任务"
    OPERATION_TERM = "重采"
    LIST_FIELDS = ["重采编号", "失败环节", "原始批次", "失败原因", "重试次数", "重采范围", "恢复游标", "重采结果"]
    REQUIRED_FIELDS = ["重采编号", "失败环节", "原始批次"]
    STATUS_ORDER = ["草稿", "待重采", "重采中", "已完成", "已放弃"]
    ACTION_RULES = {"启动重采": "待重采", "标记完成": "已完成", "标记放弃": "已放弃"}
    NEGATIVE_ACTIONS = ["标记放弃"]

    def create_retry_from_failures(self, batch_key: str) -> dict[str, Any]:
        """从解析失败的批次中创建重采任务，按失败环节归档。"""
        retry_tasks = []
        retry_counter = 0

        # 检查格式解析失败
        parse_rows = [r for r in store.rows("format_parse") if r.get("status") == "解析失败" and r.get("abnormal")]
        for pr in parse_rows:
            retry_counter += 1
            retry_tasks.append({
                "重采编号": f"RETRY-{retry_counter:05d}",
                "失败环节": "格式解析",
                "原始批次": batch_key,
                "失败原因": f"解析任务 {pr.get('任务编号', '?')} 异常失败",
                "重试次数": "0",
                "重采范围": pr.get("文档编号", "全量"),
# 数据操作：重采失败任务数据按环节归档创建重采任务
                "恢复游标": "0",
                "重采结果": "",
                "status": "待重采",
            })

        # 检查OCR失败
        ocr_rows = [r for r in store.rows("ocr_engine") if r.get("status") == "识别失败" and r.get("abnormal")]
        for ocr in ocr_rows:
            retry_counter += 1
            retry_tasks.append({
                "重采编号": f"RETRY-{retry_counter:05d}",
                "失败环节": "OCR识别",
                "原始批次": batch_key,
                "失败原因": f"识别任务 {ocr.get('任务编号', '?')} 异常失败",
                "重试次数": "0",
                "重采范围": ocr.get("图片区域", "全量"),
                "恢复游标": "0",
                "重采结果": "",
                "status": "待重采",
            })

        if not retry_tasks:
            return {"batch_key": batch_key, "retry_tasks": 0, "message": "未发现需重试的失败任务"}

        output_path = self._data_dir("retries") / f"{batch_key}_retries.jsonl"
        written = self._write_jsonl(output_path, retry_tasks)

        self._write_checkpoint(f"retry_{batch_key}", {
            "batch_key": batch_key,
            "tasks_created": len(retry_tasks),
# 数据产物：重采任务清单写入retries目录持久化文件
            "by_stage": {
                "格式解析": sum(1 for t in retry_tasks if t["失败环节"] == "格式解析"),
                "OCR识别": sum(1 for t in retry_tasks if t["失败环节"] == "OCR识别"),
            },
        })

        return {
            "batch_key": batch_key,
            "retry_tasks": len(retry_tasks),
            "records_written": written,
            "output": str(output_path),
        }

    def execute_retry(self, retry_id: str) -> dict[str, Any]:
        """执行重采任务，从上次失败位点恢复。"""
        retry_files = sorted(self._data_dir("retries").glob("*.jsonl"))
        matches: list[tuple[Any, list[dict[str, Any]], dict[str, Any]]] = []
        for rf in retry_files:
            tasks = self._read_jsonl(rf)
            for task in tasks:
                if task.get("重采编号") == retry_id:
                    matches.append((rf, tasks, task))
        # 同编号任务可能被不同批次重新派发：优先执行仍在待办/进行中的那条，
        # 全部终态时再退回重试次数最少的一条，避免旧批次记录抢占。
        active = [m for m in matches if m[2].get("status") in ("草稿", "待重采", "重采中")]
        pool = active or matches
        if pool:
            rf, tasks, task = min(
                pool, key=lambda m: int(str(m[2].get("重试次数") or "0") or 0))
# 数据产物：重试用尽边界归档记录标记放弃状态并归档数据
            # 从检查点恢复游标
            cp_name = f"retry_{retry_id}"
            cp = self._read_checkpoint(cp_name) or {"retry_id": retry_id, "attempts": 0, "offset": 0}

            # 任务记录里的重试次数是权威值：同编号任务重新派发时
            # 旧检查点不能把新任务直接判成超限。
            task_attempts = 0
            try:
                task_attempts = int(str(task.get("重试次数") or "0"))
            except ValueError:
                task_attempts = 0
            if cp.get("attempts", 0) > task_attempts or (
                task.get("status") == "待重采" and task_attempts == 0
            ):
                cp["attempts"] = task_attempts
                cp["offset"] = 0

            cp["attempts"] = cp.get("attempts", 0) + 1
            cp["offset"] = cp.get("offset", 0)
            cp["last_attempt_at"] = time.time()

            max_retries = 3
            if cp["attempts"] > max_retries:
                task["重采结果"] = f"放弃：已达最大重试次数({max_retries})"
                task["status"] = "已放弃"
                task["重试次数"] = str(cp["attempts"])
                return {"retry_id": retry_id, "status": "已放弃", "reason": f"超过{max_retries}次重试上限"}

            # 模拟重采成功
            task["重采结果"] = "成功"
            task["status"] = "已完成"
            task["重试次数"] = str(cp["attempts"])
            task["恢复游标"] = str(cp["offset"] + 100)
            self._write_checkpoint(cp_name, cp)

            # 回写任务状态
            import json
            with open(rf, "w", encoding="utf-8") as f:
                for t in tasks:
                    f.write(json.dumps(t, ensure_ascii=False, default=str) + "\n")

            return {
                "retry_id": retry_id,
                "status": "已完成",
                "attempts": cp["attempts"],
                "recovered_offset": cp["offset"] + 100,
            }

        return {"error": f"重采任务 {retry_id} 不存在"}

    def get_failure_summary(self, batch_key: str) -> dict[str, Any]:
        """获取指定批次的失败汇总，按环节分组统计失败数量和原因。"""
        # 收集各环节的失败记录
        parse_failures = [r for r in store.rows("format_parse") if r.get("status") == "解析失败"]
        ocr_failures = [r for r in store.rows("ocr_engine") if r.get("status") == "识别失败"]
        entity_failures = [r for r in store.rows("entity_recognize") if r.get("status") == "已驳回"]

        summary = {
            "batch_key": batch_key,
            "total_failures": len(parse_failures) + len(ocr_failures) + len(entity_failures),
            "by_stage": {
                "格式解析": len(parse_failures),
                "OCR识别": len(ocr_failures),
                "实体识别": len(entity_failures),
            },
            "retryable": len(parse_failures) + len(ocr_failures),
        }
        return summary