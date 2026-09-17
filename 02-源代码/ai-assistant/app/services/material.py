"""材料解析与关键字段抽取（REQ-M1-01 多格式接入 / REQ-M1-02 文档解析与字段抽取）。

两条职责，刻意分开：

1. **解析（parse）** —— 把 PDF / Excel / CSV / 纯文本统一成一段可研判的正文。
2. **抽取（extract）** —— 从正文里按**规则**捞出 REQ-M1-02 点名的关键字段，
   并算出缺失清单（REQ-M1-05 要求输出「缺失字段」）。

设计取舍
--------
* **解析库是可选依赖**。装了 `pypdf` / `pdfplumber` / `openpyxl` 就用它们；
  没装则回落到纯标准库实现（PDF 解 FlateDecode 内容流，XLSX 解
  `xl/worksheets/*.xml` + `xl/sharedStrings.xml`）。两条路产出口径一致，
  且都用 `ParseResult.parser` 标明来源 —— **不静默降级**，解析质量好不好
  在响应里一眼可见（`parser` + `warnings`）。
  这么做的原因：`requirements.txt` 刻意保持轻量（只留 fastapi/sqlalchemy 等
  基础件），不能因为「要解析 PDF」就把整个项目的依赖基调改掉；
  但客户点名的 PDF/Excel 上传又必须真的能用，所以两路并存。
* **抽取用规则而不是模型**。REQ-M1-05 要求结论「可追溯到规则条目 + 原文片段」，
  regex 抽出来的字段天然可解释、可复现；Dify 侧返回的字段与之合并，
  本地规则命中优先（本地是确定性的，Dify 侧在 mock 下是占位实现）。
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from ..core import AppError

# --------------------------------------------------------------------------- #
# 支持的文件类型（REQ-M1-01 点名 PDF / Excel，另加最常用的纯文本与 CSV）
# --------------------------------------------------------------------------- #
EXT_SOURCE_TYPE: Dict[str, str] = {
    ".pdf": "PDF",
    ".xlsx": "EXCEL",
    ".xlsm": "EXCEL",
    ".csv": "EXCEL",
    ".txt": "TEXT",
    ".md": "TEXT",
}

# 扩展到「后续可扩展 Word、图片 OCR」（SRS 括号里的规划项）时，
# 这里的提示要跟着更新，否则用户只会看到一句「不支持」。
UNSUPPORTED_HINT = (
    "支持 PDF / Excel(xlsx,csv) / 纯文本(txt,md)。"
    "老式 .xls、Word(.doc/.docx)、图片 OCR 尚未接入，请先另存为 PDF 或 xlsx 再上传。"
)

# --------------------------------------------------------------------------- #
# REQ-M1-02 点名的关键字段
# --------------------------------------------------------------------------- #
FIELD_LABELS: Dict[str, str] = {
    "name": "姓名",
    "age": "年龄",
    "degree": "学历",
    "school": "毕业院校",
    "major": "专业",
    "language": "语言成绩",
    "gpa": "均分/GPA",
    "intention_country": "意向国家",
    "intention_stage": "意向阶段",
}

# 缺失清单按这个顺序输出（先必填的硬信息，后意向类软信息）
KEY_FIELDS: Tuple[str, ...] = (
    "name", "age", "degree", "school", "major", "language",
    "gpa", "intention_country", "intention_stage",
)

# Dify 工作流侧用的键名 → 本地规范键名。
# 收敛到一套键名（与 customer_lead 表的 intention_country / intention_stage 对齐），
# 否则「同一份材料，接口和库里字段名不一样」是给未来埋坑。
_FIELD_ALIASES: Dict[str, str] = {
    "intention_country": "intention_country",
    "country": "intention_country",
    "target_country": "intention_country",
    "intention_stage": "intention_stage",
    "stage": "intention_stage",
    "name": "name",
    "student_name": "name",
    "age": "age",
    "education": "degree",
    "degree": "degree",
    "school": "school",
    "university": "school",
    "major": "major",
    "language": "language",
    "language_score": "language",
    "gpa": "gpa",
    "average_score": "gpa",
}


@dataclass
class ParseResult:
    """一次材料解析的结果。字段刻意做成可序列化的扁平结构，接口直接透出。"""

    source_type: str            # TEXT / PDF / EXCEL
    text: str
    parser: str                 # 实际用了哪个解析器，如 pypdf / openpyxl / builtin-xlsx
    filename: str
    size: int
    units: int                  # PDF 页数 / 表格数据行数 / 文本行数
    unit_name: str              # 「页」或「行」
    truncated: bool = False
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_type": self.source_type,
            "filename": self.filename,
            "size": self.size,
            "parser": self.parser,
            "units": self.units,
            "unit_name": self.unit_name,
            "chars": len(self.text),
            "truncated": self.truncated,
            "warnings": self.warnings,
            "text": self.text,
        }


# --------------------------------------------------------------------------- #
# 解析：入口
# --------------------------------------------------------------------------- #
def guess_source_type(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext not in EXT_SOURCE_TYPE:
        raise AppError(f"暂不支持的材料格式 {ext or '(无扩展名)'}。{UNSUPPORTED_HINT}")
    return EXT_SOURCE_TYPE[ext]


def parse_material(filename: str, content: bytes, *,
                   max_bytes: Optional[int] = None,
                   max_chars: Optional[int] = None) -> ParseResult:
    """解析材料二进制 → 正文。任何「读不出来」都要抛带人话的 AppError。"""
    from ..config import settings

    limit_bytes = settings.upload_max_bytes if max_bytes is None else max_bytes
    limit_chars = settings.parse_max_chars if max_chars is None else max_chars

    if not content:
        raise AppError(f"材料「{filename}」是空文件，没有可解析的内容")
    if len(content) > limit_bytes:
        raise AppError(
            f"材料「{filename}」{len(content) / 1024 / 1024:.1f} MB，"
            f"超过单份上限 {limit_bytes / 1024 / 1024:.0f} MB",
            http_status=413,
        )

    source_type = guess_source_type(filename)
    ext = Path(filename).suffix.lower()

    if source_type == "PDF":
        text, units, parser, warnings = _parse_pdf(content)
        unit_name = "页"
    elif ext == ".csv":
        text, units, parser, warnings = _parse_csv(content)
        unit_name = "行"
    elif source_type == "EXCEL":
        text, units, parser, warnings = _parse_xlsx(content)
        unit_name = "行"
    else:
        text, units, parser, warnings = _parse_plain_text(content)
        unit_name = "行"

    truncated = len(text) > limit_chars
    if truncated:
        original_chars = len(text)
        text = text[:limit_chars]
        warnings.append(f"正文 {original_chars} 字超过上限 {limit_chars} 字，已截断后送研判")

    if not text.strip():
        raise AppError(
            f"材料「{filename}」解析后没有可用文本（{parser}）。"
            "若是扫描件 / 图片型 PDF，需要走 OCR（SRS 列为后续扩展），"
            "或改为粘贴文本 / 上传 Excel。"
        )

    return ParseResult(source_type=source_type, text=_normalize_text(text),
                       parser=parser, filename=filename, size=len(content),
                       units=units, unit_name=unit_name,
                       truncated=truncated, warnings=warnings)


# --------------------------------------------------------------------------- #
# 解析：各格式实现
# --------------------------------------------------------------------------- #
def _normalize_text(text: str) -> str:
    """统一换行、去掉行尾空白、压掉连续空行。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _decode_bytes(content: bytes) -> Tuple[str, Optional[str]]:
    """UTF-8（含 BOM）→ GBK → latin-1 逐级尝试。返回 (文本, 告警)。"""
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return content.decode(encoding), None
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1"), "文本编码既不是 UTF-8 也不是 GBK，已按 latin-1 兜底，可能乱码"


def _parse_plain_text(content: bytes) -> Tuple[str, int, str, List[str]]:
    text, warning = _decode_bytes(content)
    return text, text.count("\n") + 1, "builtin-text", ([warning] if warning else [])


def _parse_csv(content: bytes) -> Tuple[str, int, str, List[str]]:
    text, warning = _decode_bytes(content)
    warnings = [warning] if warning else []
    rows: List[str] = []
    for row in csv.reader(io.StringIO(text)):
        cells = [c.strip() for c in row if c and c.strip()]
        if cells:
            rows.append(" | ".join(cells))
    if not rows:
        warnings.append("CSV 里没有非空行")
    return "\n".join(rows), len(rows), "builtin-csv", warnings


def _parse_xlsx(content: bytes) -> Tuple[str, int, str, List[str]]:
    """xlsx：优先 openpyxl，缺库则用 zipfile + ElementTree 解包（纯标准库）。"""
    errors: List[str] = []
    try:
        return _xlsx_via_openpyxl(content)
    except ImportError:
        pass
    except Exception as exc:                              # noqa: BLE001 - 换兜底实现
        errors.append(f"openpyxl 解析失败（{type(exc).__name__}: {exc}），已改用标准库兜底")
    text, rows, parser, warnings = _xlsx_builtin(content)
    return text, rows, parser, errors + warnings


def _xlsx_via_openpyxl(content: bytes) -> Tuple[str, int, str, List[str]]:
    import openpyxl                                       # type: ignore[import-not-found]

    workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    lines: List[str] = []
    for sheet in workbook.worksheets:
        lines.append(f"【工作表】{sheet.title}")
        for row in sheet.iter_rows(values_only=True):
            cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
            if cells:
                lines.append(" | ".join(cells))
    workbook.close()
    rows = sum(1 for line in lines if not line.startswith("【工作表】"))
    return "\n".join(lines), rows, "openpyxl", []


_SHEET_XML_RE = re.compile(r"^xl/worksheets/sheet\d+\.xml$")


def _xlsx_builtin(content: bytes) -> Tuple[str, int, str, List[str]]:
    """纯标准库读 xlsx：xlsx 本质是个装了 XML 的 zip。

    只认最通用的三处：`xl/sharedStrings.xml`（共享字符串）、
    `xl/worksheets/sheetN.xml`（单元格），足够覆盖机构给的简历/成绩表。
    """
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = zf.namelist()
            shared: List[str] = []
            if "xl/sharedStrings.xml" in names:
                root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                for si in root.iter(ns + "si"):
                    shared.append("".join(t.text or "" for t in si.iter(ns + "t")))

            sheet_files = sorted(n for n in names if _SHEET_XML_RE.match(n))
            if not sheet_files:
                raise AppError("这个文件看着像 xlsx，但里面没有工作表数据")

            lines: List[str] = []
            for sheet_file in sheet_files:
                ws = ET.fromstring(zf.read(sheet_file))
                for row in ws.iter(ns + "row"):
                    cells: List[str] = []
                    for cell in row.iter(ns + "c"):
                        value = cell.find(ns + "v")
                        kind = cell.get("t")
                        if kind == "s" and value is not None and value.text is not None:
                            index = int(value.text)
                            cells.append(shared[index] if index < len(shared) else "")
                        elif kind == "inlineStr":
                            cells.append("".join(t.text or "" for t in cell.iter(ns + "t")))
                        elif value is not None and value.text is not None:
                            cells.append(value.text)
                        else:
                            cells.append("")
                    cells = [c.strip() for c in cells if c and c.strip()]
                    if cells:
                        lines.append(" | ".join(cells))
    except zipfile.BadZipFile as exc:
        raise AppError("这个文件不是有效的 xlsx（zip 结构已损坏）") from exc

    if not lines:
        raise AppError("xlsx 里没有非空单元格")
    warnings = ["未安装 openpyxl，本次用标准库解包读取（只读值，不读公式结果）"]
    return "\n".join(lines), len(lines), "builtin-xlsx", warnings


def _parse_pdf(content: bytes) -> Tuple[str, int, str, List[str]]:
    """PDF：pypdf → pdfplumber → 标准库兜底。

    ⚠️ `warnings` 必须在 try 之外先初始化：两个库都「未安装」时两个分支都走
    `except ImportError: pass`，谁也没给它赋过值，后面 append 会直接
    UnboundLocalError（这个坑真实踩过，测试里有专门一条锁它）。
    """
    warnings: List[str] = []
    try:
        return _pdf_via_pypdf(content)
    except ImportError:
        pass
    except Exception as exc:                              # noqa: BLE001
        warnings.append(f"pypdf 解析失败（{type(exc).__name__}: {exc}）")

    try:
        return _pdf_via_pdfplumber(content)
    except ImportError:
        pass
    except Exception as exc:                              # noqa: BLE001
        warnings.append(f"pdfplumber 解析失败（{type(exc).__name__}: {exc}）")

    text, pages = _pdf_builtin(content)
    if warnings:
        warnings.append("已改用标准库兜底抽取正文，扫描件 / CID 子集字体的中文 PDF 可能不完整")
    else:
        warnings.append(
            "未安装 pypdf/pdfplumber，本次用标准库尽力抽取正文："
            "压缩流与拉丁字符可读，扫描件或使用 CID 子集字体的中文 PDF 会不完整")
    return text, pages, "builtin-pdf", warnings


def _pdf_via_pypdf(content: bytes) -> Tuple[str, int, str, List[str]]:
    from pypdf import PdfReader                            # type: ignore[import-not-found]

    reader = PdfReader(io.BytesIO(content))
    pages = list(reader.pages)
    chunks = [(page.extract_text() or "") for page in pages]
    return "\n".join(chunks), len(pages), "pypdf", []


def _pdf_via_pdfplumber(content: bytes) -> Tuple[str, int, str, List[str]]:
    import pdfplumber                                      # type: ignore[import-not-found]

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        chunks = [(page.extract_text() or "") for page in pdf.pages]
        return "\n".join(chunks), len(pdf.pages), "pdfplumber", []


def _inflate(raw: bytes) -> Optional[bytes]:
    """PDF 流多为 FlateDecode；解不开就说明是原始流，返回 None。"""
    for candidate in (raw, raw.strip(b"\r\n")):
        try:
            return zlib.decompress(candidate)
        except zlib.error:
            continue
    return None


def _iter_pdf_streams(content: bytes):
    for match in re.finditer(rb"stream\r?\n", content):
        start = match.end()
        end = content.find(b"endstream", start)
        if end > start:
            yield content[start:end]


_PDF_TEXT_RE = re.compile(
    rb"\((?P<lit>(?:\\.|[^\\()])*)\)\s*(?:Tj|'|\")"
    rb"|\[(?P<arr>(?:\\.|[^\]\\])*)\]\s*TJ"
    rb"|<(?P<hex>[0-9A-Fa-f\s]+)>\s*(?:Tj|'|\")"
)
_PDF_BREAK_RE = re.compile(rb"(?:T\*|Td|TD|ET)")
_PDF_PIECE_RE = re.compile(rb"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]+>")


def _decode_pdf_literal(raw: bytes) -> str:
    """解 PDF 字面量字符串：处理 \\n \\t \\( \\) \\\\ 与八进制 \\ddd。"""
    out = bytearray()
    i, size = 0, len(raw)
    while i < size:
        byte = raw[i:i + 1]
        if byte == b"\\" and i + 1 < size:
            nxt = raw[i + 1:i + 2]
            simple = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}
            if nxt in simple:
                out += simple[nxt]
                i += 2
                continue
            if nxt in (b"(", b")", b"\\"):
                out += nxt
                i += 2
                continue
            octal = re.match(rb"[0-7]{1,3}", raw[i + 1:i + 4])
            if octal:
                out.append(int(octal.group(0), 8) & 0xFF)
                i += 1 + len(octal.group(0))
                continue
            i += 2
            continue
        out += byte
        i += 1
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return out.decode("latin-1", errors="replace")


def _decode_pdf_hex(raw: bytes) -> str:
    """PDF 十六进制串。Identity-H 字体这里是字形编号，没有 ToUnicode 解不准，
    只能按 UTF-16BE 尽力而为 —— 解出来是乱码就看 warnings。"""
    digits = re.sub(rb"\s+", b"", raw)
    if len(digits) % 2:
        digits += b"0"
    try:
        data = bytes.fromhex(digits.decode("ascii"))
    except ValueError:
        return ""
    if len(data) % 2 == 0:
        try:
            return data.decode("utf-16-be")
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1", errors="replace")


def _pdf_builtin(content: bytes) -> Tuple[str, int]:
    pages = len(re.findall(rb"/Type\s*/Page[^s]", content)) or 1
    lines: List[str] = []
    for raw in _iter_pdf_streams(content):
        data = _inflate(raw)
        if data is None:
            data = raw
        if b"Tj" not in data and b"TJ" not in data:
            continue
        line: List[str] = []
        cursor = 0
        for match in _PDF_TEXT_RE.finditer(data):
            if _PDF_BREAK_RE.search(data[cursor:match.start()]) and line:
                lines.append("".join(line))
                line = []
            cursor = match.end()
            if match.group("lit") is not None:
                line.append(_decode_pdf_literal(match.group("lit")))
            elif match.group("arr") is not None:
                for piece in _PDF_PIECE_RE.finditer(match.group("arr")):
                    token = piece.group(0)
                    if token.startswith(b"("):
                        line.append(_decode_pdf_literal(token[1:-1]))
                    else:
                        line.append(_decode_pdf_hex(token[1:-1]))
            else:
                line.append(_decode_pdf_hex(match.group("hex")))
        if line:
            lines.append("".join(line))
    text = "\n".join(line for line in lines if line.strip())
    return text, pages


# --------------------------------------------------------------------------- #
# 抽取：关键字段
# --------------------------------------------------------------------------- #
_COUNTRY_ALIASES: List[Tuple[str, Tuple[str, ...]]] = [
    ("中国香港", ("中国香港", "香港")),
    ("澳大利亚", ("澳大利亚", "澳洲")),
    ("英国", ("英国",)),
    ("美国", ("美国",)),
    ("加拿大", ("加拿大",)),
    ("新加坡", ("新加坡",)),
    ("新西兰", ("新西兰",)),
    ("爱尔兰", ("爱尔兰",)),
    ("马来西亚", ("马来西亚",)),
    ("日本", ("日本",)),
    ("韩国", ("韩国",)),
    ("德国", ("德国",)),
    ("法国", ("法国",)),
    ("荷兰", ("荷兰",)),
    ("瑞士", ("瑞士",)),
    ("意大利", ("意大利",)),
]
# ASCII 别名要加词边界，否则 "uk" 会命中 "duke"、"'hk'" 命中 "shk" 之类
_COUNTRY_ASCII = {
    "英国": ("uk", "u.k.", "united kingdom", "britain"),
    "美国": ("usa", "u.s.a.", "united states", "america"),
    "加拿大": ("canada",),
    "澳大利亚": ("australia",),
    "新西兰": ("new zealand",),
    "中国香港": ("hong kong", "hk"),
    "新加坡": ("singapore",),
    "日本": ("japan",),
    "韩国": ("korea",),
}

_DEGREE_RULES: List[Tuple[str, Tuple[str, ...]]] = [
    ("博士", ("博士", "phd", "doctor")),
    ("硕士", ("硕士", "研究生", "master", "msc", "mba")),
    ("本科", ("本科", "学士", "bachelor", "undergraduate", "undergrad")),
    ("大专", ("大专", "专科", "diploma", "associate degree")),
    ("高中", ("高中", "普高", "a-level", "alevel", " ib ")),
]

_STAGE_RULES: List[Tuple[str, Tuple[str, ...]]] = [
    ("已签约", ("已签约", "已成交", "已经签约", "交了定金", "已交费", "已付定金")),
    ("待签约", ("待签约", "准备签约", "决定签约", "考虑签约", "即将签约")),
    ("比价中", ("比价", "对比机构", "对比了几家", "在对比", "货比三家")),
    ("了解中", ("了解中", "初步咨询", "刚开始了解", "了解阶段", "只是问问", "先了解")),
]

_LANGUAGE_RULES: List[Tuple[str, str, str]] = [
    ("IELTS", "雅思/IELTS", r"(?:ielts|雅思)\s*(?:总分)?\s*[：:|｜]?\s*(\d(?:\.\d)?)"),
    ("TOEFL", "托福/TOEFL", r"(?:toefl|托福)\s*[：:|｜]?\s*(\d{2,3})"),
    ("PTE", "PTE", r"\bpte\b\s*[：:|｜]?\s*(\d{2,3})"),
    ("Duolingo", "多邻国/Duolingo", r"(?:duolingo|多邻国)\s*[：:|｜]?\s*(\d{2,3})"),
    ("GRE", "GRE", r"\bgre\b\s*[：:|｜]?\s*(\d{3})"),
    ("GMAT", "GMAT", r"\bgmat\b\s*[：:|｜]?\s*(\d{3})"),
    ("JLPT", "日语/JLPT", r"(?:jlpt|日语能力)\s*[：:|｜]?\s*(n[1-5])"),
    ("TOPIK", "韩语/TOPIK", r"\btopik\b\s*[：:|｜]?\s*(\d)"),
]

# Excel 登记表常见「标签 | 值」布局，所以分隔符要同时认中英文冒号和竖线；
# 但这么放开就有个副作用：横向表头 `姓名 | 学历 | 毕业院校` 会把「学历」当成姓名。
# 用标签黑名单挡住 —— 候选值如果是另一个字段名，那就不是值。
_SEP = r"[：:|｜]"
_LABEL_WORDS = {
    "姓名", "名字", "年龄", "年纪", "学历", "毕业院校", "本科院校", "毕业学校",
    "院校", "学校", "专业", "所学专业", "语言成绩", "语言", "成绩", "均分",
    "绩点", "平均分", "gpa", "意向国家", "国家", "意向阶段", "阶段", "备注",
    "手机号", "电话", "联系方式", "邮箱", "来源", "客户", "客户姓名", "学生姓名",
    "性别", "客户信息", "基本信息", "雅思", "托福", "pte", "托福成绩",
}

_MAJOR_KEYWORDS = (
    "计算机科学与技术", "计算机", "软件工程", "人工智能", "数据科学", "大数据",
    "信息安全", "电子信息", "通信工程", "自动化", "机械工程", "机械",
    "土木工程", "建筑学", "金融学", "金融工程", "金融", "会计学", "会计",
    "财务管理", "国际经济与贸易", "工商管理", "市场营销", "人力资源",
    "传媒", "新闻", "广告", "法学", "教育学", "英语", "翻译", "心理学",
    "数学", "统计学", "物理学", "化学", "生物", "医学", "护理", "设计",
    "视觉传达", "环境设计", "音乐", "舞蹈", "表演", "酒店管理", "旅游管理",
)

_SCHOOL_SUFFIX_RE = re.compile(
    r"([\u4e00-\u9fa5]{2,10}(?:大学|学院|职业技术学院|师范学院|外国语大学))")
_EN_SCHOOL_RE = re.compile(r"([A-Z][A-Za-z.]+(?:\s+[A-Z][A-Za-z.]+){0,3}\s+(?:University|College|Institute))")

_REQUIRED_HINT: Dict[str, str] = {
    "name": "客户姓名",
    "age": "年龄",
    "degree": "当前/最高学历",
    "school": "毕业院校",
    "major": "所学专业",
    "language": "语言成绩（类型 + 分数）",
    "gpa": "均分或 GPA",
    "intention_country": "意向国家",
    "intention_stage": "意向阶段（了解中/比价中/待签约…）",
}


def _find_country(text: str) -> Optional[str]:
    lowered = text.lower()
    best: Optional[Tuple[int, str]] = None
    for country, aliases in _COUNTRY_ALIASES:
        index = -1
        for alias in aliases:
            pos = text.find(alias)
            if pos >= 0 and (index < 0 or pos < index):
                index = pos
        for alias in _COUNTRY_ASCII.get(country, ()):
            match = re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", lowered)
            if match and (index < 0 or match.start() < index):
                index = match.start()
        if index >= 0 and (best is None or index < best[0]):
            best = (index, country)
    return best[1] if best else None


def _find_language(text: str) -> Optional[Dict[str, Any]]:
    for exam, label, pattern in _LANGUAGE_RULES:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return {"exam": exam, "label": label, "score": match.group(1)}
    # 只提到考试名、没给分数：类型信息仍有价值，标成 score=None
    for exam, label, _pattern in _LANGUAGE_RULES:
        keyword = label.split("/")[0]
        if keyword in text or exam.lower() in text.lower():
            return {"exam": exam, "label": label, "score": None}
    return None


def _find_degree(text: str) -> Optional[str]:
    lowered = text.lower()
    for degree, keywords in _DEGREE_RULES:
        for keyword in keywords:
            if keyword.isascii():
                if re.search(rf"(?<![a-z]){re.escape(keyword)}(?![a-z])", lowered):
                    return degree
            elif keyword in text:
                return degree
    return None


def _find_stage(text: str) -> Optional[str]:
    for stage, keywords in _STAGE_RULES:
        if any(keyword in text for keyword in keywords):
            return stage
    return None


def _acceptable(value: Optional[str]) -> bool:
    """拦住「把表头字段名当成字段值」这种情况（横向表 `姓名 | 学历 | …`）。"""
    if not value:
        return False
    return value.strip().lower() not in _LABEL_WORDS


def _find_gpa(text: str) -> Optional[str]:
    match = re.search(
        r"(?:gpa|均分|绩点|平均分|加权平均)\s*(?:成绩)?\s*" + _SEP + r"?\s*"
        r"(\d{1,3}(?:\.\d{1,2})?(?:\s*/\s*\d{1,3}(?:\.\d{1,2})?)?)",
        text, re.IGNORECASE)
    return match.group(1).replace(" ", "") if match else None


def _find_name(text: str) -> Optional[str]:
    match = re.search(
        r"(?:客户姓名|学生姓名|姓名|名字)\s*" + _SEP + r"?\s*"
        r"([\u4e00-\u9fa5]{2,4}|[A-Za-z][A-Za-z\s]{1,30})", text)
    if match and _acceptable(match.group(1)):
        return match.group(1).strip()
    match = re.search(r"([\u4e00-\u9fa5]{2,4})\s*(?:同学|先生|女士|小姐)", text)
    if match and _acceptable(match.group(1)):
        return match.group(1)
    # 简历类纯文本：首行就是名字
    first_line = text.strip().split("\n", 1)[0].strip()
    if 2 <= len(first_line) <= 4 and re.fullmatch(r"[\u4e00-\u9fa5]{2,4}", first_line):
        return first_line
    return None


def _find_age(text: str) -> Optional[int]:
    match = re.search(r"(?:年龄|年纪)\s*" + _SEP + r"?\s*(\d{1,2})", text)
    if not match:
        match = re.search(r"(\d{1,2})\s*岁", text)
    if match:
        age = int(match.group(1))
        if 10 <= age <= 80:
            return age
    return None


def _find_school(text: str) -> Optional[str]:
    match = re.search(
        r"(?:毕业院校|本科院校|毕业学校|院校|学校)\s*" + _SEP + r"\s*"
        r"([^\s，,。;；|｜]{2,30})", text)
    if match and _acceptable(match.group(1)):
        return match.group(1).strip()
    match = _SCHOOL_SUFFIX_RE.search(text)
    if match:
        return match.group(1)
    match = _EN_SCHOOL_RE.search(text)
    if match:
        return match.group(1).strip()
    return None


def _find_major(text: str) -> Optional[str]:
    match = re.search(
        r"(?:所学专业|申请专业|本科专业|专业)\s*" + _SEP + r"\s*"
        r"([^\s，,。;；|｜]{2,20})", text)
    if match and _acceptable(match.group(1)):
        return match.group(1).strip()
    for major in _MAJOR_KEYWORDS:
        if major in text:
            return major
    return None


_CELL_SPLIT_RE = re.compile(r"\s*[|｜]\s*")


def _align_horizontal_table(text: str) -> str:
    """把「表头行 + 值行」的横向表按列名还原成「标签 | 值」纵向文本。

    Excel / CSV 导出大多是横向的：第一行是字段名，第二行才是值。
    纯 regex 没法跨列对齐（`姓名 | 学历 | 毕业院校` 后面根本没有值），
    所以先做一次列对齐，再交给字段抽取。

    只在**能确认第一行整行都是字段名**、且第二行格数与它一致时才动手；
    否则原样返回 —— 宁可少抽一个字段，也不能把正文改坏。
    """
    lines = [line for line in text.split("\n") if line.strip()]
    if len(lines) < 2:
        return text

    header = [cell.strip() for cell in _CELL_SPLIT_RE.split(lines[0].strip())]
    if len(header) < 2 or not all(cell.lower() in _LABEL_WORDS for cell in header):
        return text

    values = [cell.strip() for cell in _CELL_SPLIT_RE.split(lines[1].strip())]
    if len(values) != len(header):
        return text

    pairs = [f"{label} | {value}" for label, value in zip(header, values) if value]
    if not pairs:
        return text
    return "\n".join(pairs + lines[2:])


def extract_fields(text: str) -> Dict[str, Any]:
    """按规则抽取关键字段。只返回**确实抽到**的键，抽不到的不放占位值。

    不填「—」是因为下游要用 `missing_fields` 区分「没有」和「有但为空」；
    塞占位值会让缺失判断失真。
    """
    if not text or not text.strip():
        return {}

    text = _align_horizontal_table(text)

    fields: Dict[str, Any] = {}
    candidates = {
        "name": _find_name(text),
        "age": _find_age(text),
        "degree": _find_degree(text),
        "school": _find_school(text),
        "major": _find_major(text),
        "language": _find_language(text),
        "gpa": _find_gpa(text),
        "intention_country": _find_country(text),
        "intention_stage": _find_stage(text),
    }
    for key, value in candidates.items():
        if value is not None and value != "":
            fields[key] = value
    return fields


def normalize_fields(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把 Dify 工作流返回的字段键名收敛到本地规范键名，丢掉空值。"""
    normalized: Dict[str, Any] = {}
    for key, value in (raw or {}).items():
        canonical = _FIELD_ALIASES.get(str(key).strip().lower())
        if canonical and value not in (None, "", [], {}):
            normalized.setdefault(canonical, value)
    return normalized


def merge_fields(primary: Dict[str, Any], secondary: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """primary 优先，secondary 只补空缺（本地规则优先于 Dify 侧返回值）。"""
    merged = dict(secondary or {})
    for key, value in (primary or {}).items():
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


def missing_fields(fields: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
    """按 KEY_FIELDS 顺序算出缺失清单（REQ-M1-05 要求输出「缺失字段」）。"""
    present = fields or {}
    return [
        {"key": key, "label": FIELD_LABELS[key], "hint": _REQUIRED_HINT.get(key, "")}
        for key in KEY_FIELDS
        if present.get(key) in (None, "", [], {})
    ]


def field_rows(fields: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
    """把字段渲染成「标签 / 值」行，前端直接铺 chip。"""
    present = fields or {}
    rows: List[Dict[str, str]] = []
    for key in KEY_FIELDS:
        value = present.get(key)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, dict):
            score = value.get("score")
            text = f"{value.get('label') or value.get('exam') or ''}" + (f" {score}" if score else "")
            value = text.strip()
        rows.append({"key": key, "label": FIELD_LABELS[key], "value": str(value)})
    return rows
