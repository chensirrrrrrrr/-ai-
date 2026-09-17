# -*- coding: utf-8 -*-
"""材料解析与字段抽取的纯单元测试（REQ-M1-01 / REQ-M1-02）。

不经过 HTTP，直接打 `services/material`，把「解析」和「抽取」两条职责分开验。
"""
from __future__ import annotations

import pytest

from app.core import AppError
from app.services import material
from tests.material_fixtures import make_pdf, make_xlsx

VERTICAL_ROWS = [
    ("姓名", "赵六"),
    ("年龄", "24"),
    ("学历", "本科"),
    ("毕业院校", "西南财经大学"),
    ("专业", "金融学"),
    ("GPA", "3.4/4.0"),
    ("雅思", "7.0"),
    ("意向国家", "英国"),
    ("意向阶段", "待签约"),
]

VERTICAL_TEXT = "\n".join(
    f"{label} | {value}" for label, value in VERTICAL_ROWS)


# --------------------------------------------------------------------------- #
# 字段抽取
# --------------------------------------------------------------------------- #
def test_extract_all_key_fields_from_vertical_table():
    fields = material.extract_fields(VERTICAL_TEXT)
    assert fields["name"] == "赵六"
    assert fields["age"] == 24
    assert fields["degree"] == "本科"
    assert fields["school"] == "西南财经大学"
    assert fields["major"] == "金融学"
    assert fields["gpa"] == "3.4/4.0"
    assert fields["language"]["exam"] == "IELTS"
    assert fields["language"]["score"] == "7.0"
    assert fields["intention_country"] == "英国"
    assert fields["intention_stage"] == "待签约"
    assert material.missing_fields(fields) == []


def test_horizontal_table_is_aligned_by_header():
    """Excel/CSV 导出多是「表头行 + 值行」，要按列名对齐后再抽。"""
    text = ("姓名 | 学历 | 毕业院校 | 专业 | 意向国家\n"
            "赵六 | 本科 | 西南财经大学 | 金融学 | 英国")
    fields = material.extract_fields(text)
    assert fields["name"] == "赵六"
    assert fields["degree"] == "本科"
    assert fields["school"] == "西南财经大学"
    assert fields["major"] == "金融学"
    assert fields["intention_country"] == "英国"


def test_label_blacklist_blocks_header_as_value():
    """格数对不上就不对齐；此时也不能把表头「学历」当成姓名。"""
    fields = material.extract_fields("姓名 | 学历 | 毕业院校\n赵六")
    assert "name" not in fields
    assert material.extract_fields("毕业院校 | 专业 | 备注") .keys() == set()


def test_english_country_alias_needs_word_boundary():
    """`uk` 只认独立词，不能命中 duke / shuk 之类。"""
    assert material.extract_fields("I studied at Duke University") .get("intention_country") is None
    assert material.extract_fields("Target: UK")["intention_country"] == "英国"


def test_missing_fields_are_listed_with_labels():
    fields = material.extract_fields("意向国家：澳大利亚")
    missing = material.missing_fields(fields)
    labels = [item["label"] for item in missing]
    assert "姓名" in labels and "均分/GPA" in labels
    assert "意向国家" not in labels
    assert all(item["hint"] for item in missing), "缺失字段要带补充提示，否则用户不知道补什么"


@pytest.mark.parametrize("text,expected", [
    ("雅思 6.5", ("IELTS", "6.5")),
    ("IELTS: 7.0", ("IELTS", "7.0")),
    ("托福 105", ("TOEFL", "105")),
    ("拓福 90", None),
])
def test_language_score_variants(text, expected):
    found = material.extract_fields(text).get("language")
    if expected is None:
        assert found is None
    else:
        assert (found["exam"], found["score"]) == expected


def test_normalize_and_merge_fields_prefers_local():
    dify_side = material.normalize_fields({"country": "英国", "education": "硕士", "gpa": None})
    assert dify_side == {"intention_country": "英国", "degree": "硕士"}
    merged = material.merge_fields({"intention_country": "澳洲"}, dify_side)
    assert merged["intention_country"] == "澳洲"      # 本地规则优先
    assert merged["degree"] == "硕士"                 # 缺口由 Dify 侧补


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def test_parse_xlsx_builtin_fallback_reads_all_cells():
    """未装 openpyxl 时走标准库兜底，且必须**显式**告知用了兜底。"""
    result = material.parse_material("登记表.xlsx", make_xlsx([
        ["姓名", "意向国家", "均分"], ["孙七", "澳大利亚", "78"],
    ]))
    assert result.source_type == "EXCEL"
    assert result.parser == "builtin-xlsx"
    assert "孙七" in result.text and "澳大利亚" in result.text
    assert result.units == 2
    assert any("openpyxl" in w for w in result.warnings)


def test_parse_pdf_with_fallback_parser(monkeypatch):
    """把两个可选 PDF 库打成不可用，验证标准库兜底仍能抽出正文。"""
    def _missing(*_args, **_kwargs):
        raise ImportError("not installed")

    monkeypatch.setattr(material, "_pdf_via_pypdf", _missing)
    monkeypatch.setattr(material, "_pdf_via_pdfplumber", _missing)

    result = material.parse_material("resume.pdf", make_pdf([
        "Name: Zhao Liu", "IELTS 7.0  GPA 3.4/4.0", "Target Country: UK",
    ]))
    assert result.source_type == "PDF"
    assert result.parser == "builtin-pdf"
    assert result.units == 1
    assert "7.0" in result.text and "3.4/4.0" in result.text
    fields = material.extract_fields(result.text)
    assert fields["gpa"] == "3.4/4.0"
    assert fields["intention_country"] == "英国"
    assert any("尽力抽取" in w for w in result.warnings)


def test_parse_pdf_with_real_library_marks_parser():
    result = material.parse_material("resume.pdf", make_pdf([
        "Name: Zhao Liu", "IELTS 7.0", "Target Country: UK",
    ]))
    assert result.parser in {"pypdf", "pdfplumber", "builtin-pdf"}
    assert "7.0" in result.text


def test_parse_csv_and_gbk_text():
    csv_result = material.parse_material("list.csv", "姓名,意向国家\n李雷,美国\n".encode("utf-8"))
    assert csv_result.source_type == "EXCEL"
    assert csv_result.units == 2
    assert "李雷" in csv_result.text and "美国" in csv_result.text

    txt_result = material.parse_material("note.txt", "意向国家：日本\n".encode("gbk"))
    assert txt_result.source_type == "TEXT"
    assert txt_result.text == "意向国家：日本"
    assert txt_result.warnings == []


def test_parse_truncates_oversized_text():
    content = ("意向国家：英国\n" * 500).encode("utf-8")
    result = material.parse_material("big.txt", content, max_chars=120)
    assert result.truncated is True
    # 规整化会去掉行尾空白，所以只锁「不超过上限且接近上限」
    assert 100 <= len(result.text) <= 120
    assert any("截断" in w for w in result.warnings)


@pytest.mark.parametrize("filename,content,keyword", [
    ("old.xls", b"\xd0\xcf\x11\xe0", "暂不支持"),
    ("photo.png", b"\x89PNG\r\n", "暂不支持"),
    ("empty.txt", b"", "空文件"),
])
def test_parse_rejects_unsupported_and_empty(filename, content, keyword):
    with pytest.raises(AppError) as err:
        material.parse_material(filename, content)
    assert keyword in err.value.message


def test_parse_rejects_oversized_file():
    with pytest.raises(AppError) as err:
        material.parse_material("big.pdf", b"%PDF-1.4" + b"0" * 2048, max_bytes=1024)
    assert err.value.http_status == 413


def test_parse_rejects_xlsx_without_sheet():
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("xl/sharedStrings.xml", '<?xml version="1.0"?><sst/>')
    with pytest.raises(AppError) as err:
        material.parse_material("broken.xlsx", buffer.getvalue())
    assert "工作表" in err.value.message


def test_guess_source_type_maps_extension():
    assert material.guess_source_type("a.PDF") == "PDF"
    assert material.guess_source_type("a.xlsm") == "EXCEL"
    assert material.guess_source_type("a.csv") == "EXCEL"
    assert material.guess_source_type("a.md") == "TEXT"
