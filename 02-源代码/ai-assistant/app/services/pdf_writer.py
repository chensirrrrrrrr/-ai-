"""用标准库写 PDF（报告导出用，零依赖）。

为什么自己写：`reportlab` / `weasyprint` 都是重依赖（weasyprint 还要系统级
pango/cairo），本项目刻意保持「装完 fastapi 就能跑」的基调；而报告导出只需要
「多页 + 标题 + 段落 + 表格」这点排版能力。

中文怎么处理（关键取舍）
------------------------
PDF 规范里有一套**预定义 CJK 字体**机制：`/BaseFont /STSong-Light` +
`/Encoding /UniGB-UCS2-H`，正文里直接写 UCS-2BE 编码的十六进制串，
由阅读器提供字形 —— **不需要嵌入字体文件**（嵌入一个中文字体动辄 10 MB）。
代价是「字形由阅读器给」：Acrobat / Foxit / WPS / Edge 这类主流阅读器都自带
Adobe-GB1 字体或会按字符集回退到系统中文（Windows 上是 SimSun），能正常显示。

为了让**文本可被程序提取**（不依赖阅读器），这里额外写了一份 `/ToUnicode` CMap：
因为字节流本来就是 UCS-2 码位，映射是恒等的，`pypdf` 之类的解析器能拿到正确中文。
`tests/test_reports.py` 里就是用 pypdf 回读来验证结构 + 编码都对的。

如果目标环境确实需要「字体完全内嵌、任何阅读器都一模一样」的 PDF，
正确做法是引入字体子集化方案（fonttools + 中文字体），属于部署期决策，
不该悄悄塞进这个演示项目 —— 这里是显式说明而不是假装没有这个问题。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, List, Sequence, Tuple

# A4（pt）
PAGE_W = 595.28
PAGE_H = 841.89
MARGIN = 46.0
CONTENT_W = PAGE_W - MARGIN * 2
FOOTER_H = 26.0

FONT = "F1"
SIZE_TITLE = 17.0
SIZE_H2 = 12.5
SIZE_BODY = 10.0
SIZE_TABLE = 9.0
SIZE_FOOTER = 8.0

LINE_BODY = 15.5
LINE_TABLE = 14.0


def _hex(text: str) -> str:
    """把文本编码成 PDF 字符串（UCS-2BE 十六进制串）。非 BMP 字符降级为 '?'。"""
    codes: List[str] = []
    for char in str(text):
        code = ord(char)
        if char in "\t":
            char, code = " ", 0x20
        if code < 0x20 or code > 0xFFFF:
            code = ord("?")
        codes.append(f"{code:04X}")
    return "<" + "".join(codes) + ">" if codes else "<0020>"


def _is_wide(char: str) -> bool:
    return ord(char) > 0x2E80


def text_width(text: str, size: float) -> float:
    """粗略宽度：CJK 按 1 em，其他按 0.52 em（够排版用，不需要真实字体度量）。"""
    return sum(size * (1.0 if _is_wide(c) else 0.52) for c in str(text))


def wrap(text: str, size: float, max_width: float) -> List[str]:
    """按宽度折行。中文没有空格，所以按字符贪心切；英文尽量不切断单词。"""
    lines: List[str] = []
    current = ""
    for token in _tokenize(str(text)):
        if current and text_width(current + token, size) > max_width:
            lines.append(current)
            current = token.lstrip()
        else:
            current += token
        while current and text_width(current, size) > max_width:
            # 单个 token 就超宽（超长英文/无空格串）→ 硬切
            cut = _hard_cut(current, size, max_width)
            lines.append(current[:cut])
            current = current[cut:]
    if current:
        lines.append(current)
    return lines or [""]


def _tokenize(text: str) -> List[str]:
    """把文本切成「一个中文字 / 一个英文单词+后随空格」的 token 流。"""
    tokens: List[str] = []
    buffer = ""
    for char in text:
        if _is_wide(char):
            if buffer:
                tokens.append(buffer)
                buffer = ""
            tokens.append(char)
        elif char == " ":
            buffer += char
            tokens.append(buffer)
            buffer = ""
        else:
            buffer += char
    if buffer:
        tokens.append(buffer)
    return tokens


def _hard_cut(text: str, size: float, max_width: float) -> int:
    index = 0
    width = 0.0
    while index < len(text):
        step = size * (1.0 if _is_wide(text[index]) else 0.52)
        if width + step > max_width:
            break
        width += step
        index += 1
    return max(1, index)


class _Builder:
    def __init__(self) -> None:
        self._pages: List[List[str]] = []
        self._ops: List[str] = []
        self._y = PAGE_H - MARGIN
        self._started = False

    # ---------- 页面管理 ----------
    def _flush(self) -> None:
        """把当前页收进 `_pages`，然后开一页新的（内容为空时不产生空白页）。

        ⚠️ 这里**不能**用 `_started` 之类的标志位做条件：只有一页、且从未溢出的报告
        会因此一页都收不进去（实测 pypdf 报 pages=0）。判据只能是「当前页有没有内容」。
        """
        if self._ops:
            self._pages.append(self._ops)
        self._ops = []
        self._y = PAGE_H - MARGIN
        self._started = True

    def _ensure(self, height: float) -> None:
        if self._y - height < MARGIN + FOOTER_H:
            self._flush()

    # ---------- 绘制原语 ----------
    def _draw(self, x: float, y: float, size: float, text: str) -> None:
        self._ops.append(
            f"BT /{FONT} {size:.2f} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm {_hex(text)} Tj ET")

    def _rect(self, x: float, y: float, width: float, height: float,
              gray: float = 0.94) -> None:
        self._ops.append(f"{gray:.3f} g {x:.2f} {y:.2f} {width:.2f} {height:.2f} re f 0 g")

    # ---------- 语义块 ----------
    def title(self, text: str) -> None:
        self._ensure(SIZE_TITLE + 12)
        self._y -= SIZE_TITLE + 4
        self._draw(MARGIN, self._y, SIZE_TITLE, text)
        self._y -= 8
        self._ops.append(f"0.6 g {MARGIN:.2f} {self._y:.2f} {CONTENT_W:.2f} 0.8 re f 0 g")
        self._y -= 6

    def heading(self, text: str) -> None:
        self._ensure(SIZE_H2 + LINE_BODY + 8)
        self._y -= SIZE_H2 + 8
        self._draw(MARGIN, self._y, SIZE_H2, text)
        self._y -= 6

    def paragraph(self, text: str, *, indent: float = 0.0, size: float = SIZE_BODY) -> None:
        for line in wrap(text, size, CONTENT_W - indent):
            self._ensure(LINE_BODY)
            self._y -= LINE_BODY
            self._draw(MARGIN + indent, self._y, size, line)

    def bullet(self, text: str) -> None:
        self.paragraph("· " + text, indent=10.0)

    def key_values(self, pairs: Sequence[Tuple[str, Any]]) -> None:
        for label, value in pairs:
            self.paragraph(f"{label}：{value}")

    def spacer(self, height: float = 6.0) -> None:
        self._y -= height

    def table(self, columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
        columns = list(columns)
        if not columns:
            return
        cells = [[_cell_text(value) for value in row] for row in rows]

        # 列宽按内容宽度分配，再整体缩放到可用宽度
        natural = [text_width(name, SIZE_TABLE) + 12 for name in columns]
        for row in cells:
            for index, value in enumerate(row[:len(columns)]):
                natural[index] = max(natural[index], text_width(value, SIZE_TABLE) + 12)
        scale = min(1.0, CONTENT_W / sum(natural)) if sum(natural) else 1.0
        widths = [max(34.0, w * scale) for w in natural]
        if sum(widths) > CONTENT_W:                      # 缩放后仍超出 → 等比压回
            factor = CONTENT_W / sum(widths)
            widths = [w * factor for w in widths]
        total_w = sum(widths)

        self._ensure(LINE_TABLE * 2)
        self._y -= LINE_TABLE
        self._rect(MARGIN, self._y - 4.0, total_w, LINE_TABLE)
        x = MARGIN
        for index, name in enumerate(columns):
            self._draw(x + 6, self._y, SIZE_TABLE, _clip(name, SIZE_TABLE, widths[index] - 10))
            x += widths[index]

        for row in cells:
            self._ensure(LINE_TABLE)
            self._y -= LINE_TABLE
            x = MARGIN
            for index in range(len(columns)):
                value = row[index] if index < len(row) else ""
                self._draw(x + 6, self._y, SIZE_TABLE,
                           _clip(value, SIZE_TABLE, widths[index] - 10))
                x += widths[index]
        self._y -= 6

    # ---------- 收尾 ----------
    def build(self) -> List[List[str]]:
        self._flush()
        total = len(self._pages)
        for index, ops in enumerate(self._pages, start=1):
            ops.append(
                f"0.45 g BT /{FONT} {SIZE_FOOTER:.2f} Tf 1 0 0 1 {MARGIN:.2f} "
                f"{MARGIN - 12:.2f} Tm {_hex(f'第 {index} 页 / 共 {total} 页')} Tj ET 0 g")
        return self._pages


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return str(value)


def _clip(text: str, size: float, max_width: float) -> str:
    if text_width(text, size) <= max_width:
        return text
    out = ""
    for char in text:
        if text_width(out + char, size) > max_width - size * 0.6:
            break
        out += char
    return out + "…"


def _to_unicode_cmap() -> str:
    """恒等映射的 ToUnicode CMap：字节流本来就是 UCS-2 码位。"""
    return """\
/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<0000> <FFFF>
endcodespacerange
1 beginbfrange
<0020> <FFE5> <0020>
endbfrange
endcmap
CMapName currentdict /CMap defineresource pop
end
end
"""


class _PdfFile:
    """极简 PDF 对象表：只支持「顺序写对象」+「按需回填引用」。"""

    def __init__(self) -> None:
        self._objects: List[bytes] = []

    def add(self, body: bytes) -> int:
        self._objects.append(body)
        return len(self._objects)          # 对象号从 1 开始

    def build(self, root: int) -> bytes:
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: List[int] = []
        for number, body in enumerate(self._objects, start=1):
            offsets.append(len(out))
            out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
        xref_at = len(out)
        count = len(self._objects) + 1
        out += f"xref\n0 {count}\n".encode()
        out += b"0000000000 65535 f \n"
        for offset in offsets:
            out += f"{offset:010d} 00000 n \n".encode()
        out += (f"trailer\n<< /Size {count} /Root {root} 0 R >>\n"
                f"startxref\n{xref_at}\n%%EOF\n").encode()
        return bytes(out)


def write_pdf(*, title: str, blocks: Iterable[Tuple[str, Any]]) -> bytes:
    """把「语义块」渲染成 PDF。

    blocks 是 `(kind, payload)` 序列，kind ∈ heading | paragraph | bullet
    | table | keyvalues | spacer，payload 对应各自的参数。
    调用方（`services/reports.render_pdf`）负责把报告快照翻译成这些块，
    这样 PDF 层完全不需要知道「报告」是什么。
    """
    builder = _Builder()
    builder.title(title)
    for kind, payload in blocks:
        if kind == "heading":
            builder.heading(payload)
        elif kind == "paragraph":
            builder.paragraph(payload)
        elif kind == "bullet":
            builder.bullet(payload)
        elif kind == "keyvalues":
            builder.key_values(payload)
        elif kind == "table":
            builder.table(payload[0], payload[1])
        elif kind == "spacer":
            builder.spacer(payload)
        else:
            raise ValueError(f"未知的 PDF 块类型：{kind}")

    pages = builder.build()
    pdf = _PdfFile()
    catalog = pdf.add(b"")                 # 占位，最后回填
    pages_id = pdf.add(b"")                # 占位

    # 先加被引用的对象，再写引用者 —— 对象号直接来自 add()，不做任何 +N 推算
    cid_id = pdf.add(
        b"<</Type/Font/Subtype/CIDFontType0/BaseFont/STSong-Light"
        b"/CIDSystemInfo<</Registry(Adobe)/Ordering(GB1)/Supplement 2>>>>")
    cmap = _to_unicode_cmap().encode("ascii")
    cmap_id = pdf.add(b"<</Length " + str(len(cmap)).encode() + b">>stream\n" + cmap
                      + b"\nendstream")
    font_id = pdf.add(
        f"<</Type/Font/Subtype/Type0/BaseFont/STSong-Light/Encoding/UniGB-UCS2-H"
        f"/DescendantFonts[{cid_id} 0 R]/ToUnicode {cmap_id} 0 R>>".encode())

    page_ids: List[int] = []
    for ops in pages:
        content = ("\n".join(ops) + "\n").encode("ascii")
        content_id = pdf.add(b"<</Length " + str(len(content)).encode()
                             + b">>stream\n" + content + b"\nendstream")
        page_ids.append(pdf.add(
            f"<</Type/Page/Parent {pages_id} 0 R/MediaBox[0 0 {PAGE_W:.2f} {PAGE_H:.2f}]"
            f"/Resources<</Font<</{FONT} {font_id} 0 R>>>>/Contents {content_id} 0 R>>"
            .encode()))

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    pdf._objects[pages_id - 1] = (f"<</Type/Pages/Kids[{kids}]/Count {len(page_ids)}>>"
                                  .encode())
    pdf._objects[catalog - 1] = f"<</Type/Catalog/Pages {pages_id} 0 R>>".encode()
    return pdf.build(catalog)
