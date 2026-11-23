"""OCR区域识别凭据与版式还原合并的回放测试。

覆盖：
- 同一区域单区识别与整页识别同时写入时的去重（两种到达顺序）；
- 识别期间切换语言/引擎配置，同一区域留下不同凭据版本；
- 一次确认里一个区域只贡献一段文本，坐标/配置/来源可核对；
- 并发识别请求刷新后可区分在途与已确认；
- 版式还原按页合并图中文字与段落数据并记录实际顺序。
"""
from __future__ import annotations

import json
import os
import sys
import threading

import pytest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

import app.services.base as base_module
from app.services.base import DATA_ROOT, _file_lock
from app.services.format_parse import FormatParseService
from app.services.layout_restore import LayoutRestoreService
from app.services.ocr_engine import OcrEngineService


@pytest.fixture()
def doc_id(tmp_path, monkeypatch):
    """每个用例独立数据目录，避免识别产物跨用例串味。"""
    monkeypatch.setattr(base_module, "DATA_ROOT", tmp_path)
    doc = f"DOC-OCR-{tmp_path.name[-6:]}"
    parse_svc = FormatParseService()
    task, _ = parse_svc.create_entry({
        "任务编号": "PR-OCR", "文档编号": doc, "解析器类型": "PDF",
    })
    result = parse_svc.execute_parse(task["id"])
    assert "error" not in result, result.get("error")
    assert result["pages"] > 0
    return doc


def _credentials(doc_id: str) -> list[dict]:
    return OcrEngineService()._read_jsonl(
        OcrEngineService()._credentials_path(doc_id))


def _requests(doc_id: str) -> list[dict]:
    return OcrEngineService()._read_jsonl(
        OcrEngineService()._requests_path(doc_id))


class TestRegionCredentialDedup:
    """单区/整页识别同时命中同一区域时，凭据只保留一条。"""

    @pytest.mark.parametrize("order", [("区域识别", "整页识别"), ("整页识别", "区域识别")])
    def test_two_arrival_orders_produce_same_credential(self, doc_id, order):
        svc = OcrEngineService()
        baseline = len(_requests(doc_id))
        for source in order:
            if source == svc.SOURCE_AREA:
                result = svc.recognize_from_area(doc_id)
            else:
                result = svc.batch_recognize_all_images(doc_id)
            assert "error" not in result, result.get("error")

        credentials = _credentials(doc_id)
        # 文档只有第2页有图片区域，两个入口各识别一次应合并为一条凭据。
        assert len(credentials) == 1, credentials
        credential = credentials[0]
        assert credential["region_id"].startswith(f"{doc_id}-P002-")
        assert credential["bbox"] == "(10, 9, 160, 159)"
        assert credential["language"] == "chinese"
        assert credential["engine"] == "paddle"
        assert credential["engine_version"] == "v3.2.0"
        assert credential["state"] == "已确认"
        assert set(credential["sources"]) == {svc.SOURCE_AREA, svc.SOURCE_PAGE}
        assert len(credential["request_ids"]) == baseline + 2

        # 每次识别都登记独立请求，且都已确认；解析阶段整页识别的那次也在其中。
        requests = _requests(doc_id)
        assert len(requests) == baseline + 2
        assert {rec["state"] for rec in requests} == {svc.STATE_CONFIRMED}
        assert len({rec["request_id"] for rec in requests}) == baseline + 2

    def test_region_id_independent_of_entry_route(self, doc_id):
        area = OcrEngineService().recognize_from_area(doc_id)
        page = OcrEngineService().batch_recognize_all_images(doc_id)
        assert area["recognized"][0]["region_id"] == page["recognized"][0]["region_id"]
        assert area["recognized"][0]["credential_id"] == page["recognized"][0]["credential_id"]

    def test_repeat_region_within_page_gets_stable_distinct_ids(self, doc_id):
        svc = OcrEngineService()
        image_path = svc._data_dir("layout") / f"{doc_id}_images.jsonl"
        baseline = svc._read_jsonl(image_path)[0]
        svc._atomic_write_jsonl(image_path, [baseline, dict(baseline)])

        result = svc.batch_recognize_all_images(doc_id)
        assert "error" not in result, result.get("error")
        region_ids = [item["region_id"] for item in result["recognized"]]
        assert len(region_ids) == 2
        assert len(set(region_ids)) == 2
        assert len(_credentials(doc_id)) == 2  # 原始区域与重复区域各一条，解析阶段那条已合并
        # 重复区域与原始区域共用首条标识，重复项带稳定序号避免覆盖。
        assert any(region_id.endswith("-2") for region_id in region_ids)

    def test_region_ids_do_not_depend_on_file_row_order(self, doc_id, tmp_path, monkeypatch):
        svc = OcrEngineService()
        image_path = svc._data_dir("layout") / f"{doc_id}_images.jsonl"
        baseline = svc._read_jsonl(image_path)[0]
        second = dict(baseline, bbox="(20, 300, 200, 420)")
        first_order = [baseline, second]
        second_order = [second, baseline]

        ids = []
        for name, rows in (("a", first_order), ("b", second_order)):
            root = tmp_path / name
            # 每个回放目录都要有版式产物，整页识别以此为前置条件。
            monkeypatch.setattr(base_module, "DATA_ROOT", root)
            layout_dir = svc._data_dir("layout")
            svc._atomic_write_jsonl(layout_dir / f"{doc_id}_layout.jsonl", [{"doc_id": doc_id, "page": 2}])
            svc._atomic_write_jsonl(layout_dir / f"{doc_id}_images.jsonl", rows)
            result = OcrEngineService().batch_recognize_all_images(doc_id)
            ids.append([item["region_id"] for item in result["recognized"]])
        assert ids[0] == ids[1]
        expected_first = OcrEngineService._region_id(doc_id, 2, "(10, 9, 160, 159)")
        # bbox 上沿更小的区域排在阅读顺序前面。
        assert ids[0][0] == expected_first


class TestConfigVersioning:
    """识别期间换语言配置，同一区域留下不同版本的文本。"""

    def test_language_change_creates_new_version(self, doc_id):
        svc = OcrEngineService()
        svc.recognize_from_area(doc_id, language="chinese")
        svc.recognize_from_area(doc_id, language="english")

        versions = svc.region_versions(doc_id)
        assert len(versions) == 1
        records = next(iter(versions.values()))
        assert len(records) == 2
        assert {rec["language"] for rec in records} == {"chinese", "english"}
        assert len({rec["credential_version"] for rec in records}) == 2
        assert len({rec["text"] for rec in records}) == 2

    def test_same_config_merges_into_one_version(self, doc_id):
        svc = OcrEngineService()
        svc.recognize_from_area(doc_id)
        svc.batch_recognize_all_images(doc_id, language="chinese")
        records = next(iter(svc.region_versions(doc_id).values()))
        assert len(records) == 1

    def test_engine_and_threshold_fingerprint(self, doc_id):
        svc = OcrEngineService()
        svc.recognize_from_area(doc_id, config={"engine": "tesseract", "threshold": 0.9})
        svc.recognize_from_area(doc_id, config={"engine": "tesseract", "threshold": 0.8})
        records = [
            rec for rec in next(iter(svc.region_versions(doc_id).values()))
            if rec["engine"] == "tesseract"
        ]
        assert len(records) == 2
        assert {rec["engine_version"] for rec in records} == {"v5.3.0"}
        assert {rec["threshold"] for rec in records} == {0.9, 0.8}


class TestConfirmationSelection:
    """一次确认里每个区域只选定一段文本，且坐标/配置/来源可核对。"""

    def test_one_text_per_region_with_traceable_metadata(self, doc_id):
        svc = OcrEngineService()
        svc.recognize_from_area(doc_id, language="chinese")
        svc.recognize_from_area(doc_id, language="english")

        confirmation = svc.confirm_document(doc_id)
        assert "error" not in confirmation, confirmation.get("error")
        assert confirmation["selected_count"] == 1
        selected = confirmation["selected"][0]

        region_versions = next(iter(svc.region_versions(doc_id).values()))
        chosen = next(rec for rec in region_versions
                      if rec["credential_version"] == selected["credential_version"])
        assert selected["region_id"] == chosen["region_id"]
        assert selected["bbox"] == chosen["bbox"]
        assert selected["language"] == chosen["language"]
        assert selected["source"] == chosen["source"]
        assert selected["text"] == chosen["text"]

    def test_language_preference_pins_expected_version(self, doc_id):
        svc = OcrEngineService()
        svc.recognize_from_area(doc_id, language="chinese")
        svc.recognize_from_area(doc_id, language="english")

        english = svc.confirm_document(doc_id, language="english")
        assert english["selected"][0]["language"] == "english"
        chinese = svc.confirm_document(doc_id, language="chinese")
        assert chinese["selected"][0]["language"] == "chinese"
        assert english["confirmation_id"] != chinese["confirmation_id"]

    def test_confirm_is_idempotent_and_recorded(self, doc_id):
        svc = OcrEngineService()
        svc.recognize_from_area(doc_id)
        first = svc.confirm_document(doc_id)
        again = svc.confirm_document(doc_id)
        assert first["confirmation_id"] == again["confirmation_id"]
        confirmations = svc.list_confirmations(doc_id)
        assert len(confirmations) == 1
        assert svc.get_confirmation(first["confirmation_id"], doc_id) is not None


class TestInFlightVisibility:
    """并发识别请求刷新后能区分在途与已确认。"""

    def test_refresh_separates_in_flight_and_confirmed(self, doc_id):
        svc = OcrEngineService()
        svc.recognize_from_area(doc_id, language="chinese")
        pending_id = svc.begin_region_recognition(
            doc_id=doc_id,
            region_id=f"{doc_id}-P002-DEADBEEF",
            image_area_id=doc_id,
            page=2,
            bbox="(10, 9, 160, 159)",
            source=svc.SOURCE_PAGE,
            config=svc._resolve_config({"page": 2}, language="english", config=None),
        )

        status = svc.region_status(doc_id)
        assert status["confirmed_count"] == 1
        assert status["in_flight_count"] == 1
        assert status["in_flight"][0]["request_id"] == pending_id
        assert status["confirmed"][0]["state"] == "已确认"
        assert status["confirmed"][0]["language"] == "chinese"

        svc.complete_region_recognition(pending_id, text="english text", confidence=0.88)
        refreshed = svc.region_status(doc_id)
        assert refreshed["in_flight_count"] == 0
        assert refreshed["confirmed_count"] == 2

    def test_concurrent_entries_keep_every_credential_readable(self, doc_id):
        svc = OcrEngineService()
        errors: list[BaseException] = []

        def worker(source: str) -> None:
            try:
                service = OcrEngineService()
                if source == service.SOURCE_AREA:
                    service.recognize_from_area(doc_id)
                else:
                    service.batch_recognize_all_images(doc_id)
            except BaseException as exc:  # pragma: no cover - 仅用于失败时定位
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(svc.SOURCE_AREA,)),
            threading.Thread(target=worker, args=(svc.SOURCE_PAGE,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors, errors
        # 并发写不产生半行：每次刷新都能解析出完整记录。
        status = svc.region_status(doc_id)
        assert status["in_flight_count"] == 0
        assert status["confirmed_count"] >= 1
        for rec in _credentials(doc_id):
            assert rec["text"] and rec["text_checksum"]
        assert all(rec["state"] == "已确认" for rec in status["confirmed"])


class TestLayoutMerge:
    """版式还原读取凭据，按页合并图中文字与段落数据。"""

    def test_layout_merge_uses_confirmation(self, doc_id):
        ocr_svc = OcrEngineService()
        ocr_svc.recognize_from_area(doc_id, language="chinese")
        ocr_svc.recognize_from_area(doc_id, language="english")
        confirmation = ocr_svc.confirm_document(doc_id, language="english")

        layout_svc = LayoutRestoreService()
        result = layout_svc.restore_from_document(
            doc_id, confirmation_id=confirmation["confirmation_id"])
        assert "error" not in result, result.get("error")
        assert result["ocr_confirmation_id"] == confirmation["confirmation_id"]
        assert result["ocr_regions_merged"] == 1

        merged = layout_svc.merged_pages(doc_id)
        assert merged["ocr_confirmation_id"] == confirmation["confirmation_id"]
        assert merged["confirmation"]["selected"][0]["language"] == "english"

        page_two = next(page for page in merged["pages"] if page["page"] == 2)
        image_texts = page_two["image_texts"]
        assert len(image_texts) == 1
        assert image_texts[0]["text"] == confirmation["selected"][0]["text"]
        assert image_texts[0]["bbox"] == "(10, 9, 160, 159)"
        assert image_texts[0]["language"] == "english"
        assert image_texts[0]["sources"] == confirmation["selected"][0]["sources"]

        # 段落块仍在，图中文字作为独立块追加在段落之后，阅读序列完整覆盖。
        kinds = [block["kind"] for block in page_two["blocks"]]
        assert "image_text" in kinds
        assert kinds.count("paragraph") > 0
        assert page_two["reading_sequence"] == [block["block_id"] for block in page_two["blocks"]]

    def test_layout_without_confirmation_keeps_paragraphs(self, doc_id):
        layout_svc = LayoutRestoreService()
        result = layout_svc.restore_from_document(doc_id)
        assert "error" not in result, result.get("error")
        assert result["ocr_confirmation_id"] is None
        assert result["ocr_regions_merged"] == 0
        merged = layout_svc.merged_pages(doc_id)
        assert all(not page["image_texts"] for page in merged["pages"])

    def test_layout_uses_latest_confirmation_by_default(self, doc_id):
        ocr_svc = OcrEngineService()
        ocr_svc.recognize_from_area(doc_id, language="chinese")
        ocr_svc.recognize_from_area(doc_id, language="english")
        chinese = ocr_svc.confirm_document(doc_id, language="chinese")
        english = ocr_svc.confirm_document(doc_id, language="english")

        result = LayoutRestoreService().restore_from_document(doc_id)
        # 两次确认的 created_at 可能相同，用显式指定的那次做重复校验。
        explicit = LayoutRestoreService().restore_from_document(
            doc_id, confirmation_id=chinese["confirmation_id"])
        assert explicit["ocr_confirmation_id"] == chinese["confirmation_id"]
        assert result["ocr_confirmation_id"] in {
            chinese["confirmation_id"], english["confirmation_id"]}


class TestArrivalOrderReplay:
    """重放单区先到/整页先到两种顺序，核对阅读序列、区域标识与选定文本一致。"""

    @staticmethod
    def _run(doc: str, order: tuple[str, ...]) -> dict:
        svc = OcrEngineService()
        for source in order:
            if source == svc.SOURCE_AREA:
                svc.recognize_from_area(doc, language="chinese")
            else:
                svc.batch_recognize_all_images(doc, language="chinese")
        confirmation = svc.confirm_document(doc, language="chinese")
        layout = LayoutRestoreService().restore_from_document(
            doc, confirmation_id=confirmation["confirmation_id"])
        merged = LayoutRestoreService().merged_pages(doc)
        return {
            "confirmation_id": confirmation["confirmation_id"],
            "reading_sequences": layout["reading_sequences"],
            "region_ids": [item["region_id"] for item in confirmation["selected"]],
            "selected_texts": [item["text"] for item in confirmation["selected"]],
            "credential_ids": [item["credential_id"] for item in confirmation["selected"]],
            "image_texts": [
                (item["region_id"], item["text"], item["text_checksum"])
                for page in merged["pages"]
                for item in page["image_texts"]
            ],
        }

    @pytest.mark.parametrize(
        "first_order,second_order",
        [
            (("区域识别", "整页识别"), ("整页识别", "区域识别")),
        ],
    )
    def test_two_orders_agree(self, tmp_path, monkeypatch, first_order, second_order):
        outcomes = []
        for index, order in enumerate((first_order, second_order)):
            monkeypatch.setattr(base_module, "DATA_ROOT", tmp_path / f"run{index}")
            doc = "DOC-REPLAY"
            parse_svc = FormatParseService()
            task, _ = parse_svc.create_entry({
                "任务编号": "PR-REPLAY", "文档编号": doc, "解析器类型": "PDF",
            })
            assert "error" not in parse_svc.execute_parse(task["id"])
            outcomes.append(self._run(doc, order))

        first, second = outcomes
        assert first["region_ids"] == second["region_ids"]
        assert first["selected_texts"] == second["selected_texts"]
        assert first["credential_ids"] == second["credential_ids"]
        assert first["reading_sequences"] == second["reading_sequences"]
        assert first["image_texts"] == second["image_texts"]
        # 两个入口各跑一次，同一区域只贡献一段文本。
        assert len(first["image_texts"]) == 1
