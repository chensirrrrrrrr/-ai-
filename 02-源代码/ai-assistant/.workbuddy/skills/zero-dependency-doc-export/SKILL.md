---
name: zero-dependency-doc-export
description: 不引第三方库，用标准库导出 Excel(xlsx) 与 PDF（报告/报表/单据场景）。含 xlsx 的最小 XML 结构、PDF 的最小对象表与分页、以及「PDF 预定义 CJK 字体在 Chrome/Edge 里渲染成乱码」这个必踩的坑和三条可选出路。当项目要求「导出 Excel/PDF」但又不想背 openpyxl/reportlab/weasyprint 这些依赖时使用。
agent_created: true
---

# 零依赖导出 xlsx / PDF

适用：报告、报表、对账单这类「排版不复杂、但要能下载」的导出需求。
本机/生产都不方便装 `openpyxl`、`reportlab`、`weasyprint`（后者还要系统级 pango/cairo）时，
两种格式都能用标准库写出来 —— 但要避开下面这些坑。

---

## 一、xlsx：本质是个装了 XML 的 zip

最小可用结构（缺一个 Excel 就打不开或报修复）：

```
[Content_Types].xml
_rels/.rels
xl/workbook.xml
xl/_rels/workbook.xml.rels
xl/styles.xml
xl/worksheets/sheet1.xml      # 多个 sheet 就多个文件
```

要点：

1. **字符串用 inlineStr 最省事**：`<c r="A1" t="inlineStr"><is><t xml:space="preserve">文本</t></is></c>`。
   可以完全绕开 `sharedStrings.xml` 和下标管理 —— 导出是一次性的，不需要共享字符串省空间。
   忘了 `xml:space="preserve"` 会吃掉首尾空格。
2. **数字不要加 `t`**：`<c r="B1"><v>12</v></c>`；布尔写 `t="b"` + `1/0`。
   把数字写成字符串，Excel 左上角会冒绿三角、还不能求和。
3. **转义**：`& < >` 必须转（`xml.sax.saxutils.escape`）；`"` `'` 在文本节点里可以不转。
4. **列宽按显示宽度算**：CJK 按 2、其他按 1，取每列最长值夹在 [9, 60]。
   不算列宽的话中文列会显示成 `###`。
5. **冻结首行**：`<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>`。
6. 行号从 **1** 开始；列名用 `A..Z, AA..`；`sheet name` 有 31 字符上限且不能含 `[]:*?/\`。
7. 表头加粗只需 `styles.xml` 里一个 `cellXfs` + `s="1"`，不必搞全套样式。

**怎么验证（装不了 openpyxl 时）**：写一个的读端，或直接用同一项目里已有的 xlsx 读端做
**往返断言** —— 写进去的每个单元格都要能原样读回来（中文、负数、特殊字符各来一条）。

---

## 二、PDF：手写对象表 + 内容流

结构（版式简单的话足够）：

```
1 Catalog → 2 Pages → N 个 Page（每个带自己的 Contents 流）
字体对象：Type0 / BaseFont / Encoding / DescendantFonts / ToUnicode
结尾 xref 表 + trailer
```

**必须做对的几件事**（错一个就是「打不开」或「页数为 0」）：

1. **`/Length` 必须等于内容流真实字节数**，`xref` 偏移必须对。偷懒写错的话
   `pdfplumber`/`pypdf` 会越界读到下一个对象，正文变乱码，然后你会花半小时怀疑自己的解析器。
2. **分页收集器的守卫条件不能是标志位**。典型 bug：
   `if self._started: self._pages.append(self._ops)` —— 只有一页、从未溢出的文档
   会**一页都收不进去**，`pypdf` 报 `pages=0`。判据只能是「当前页有没有内容」。
3. **引用对象先加、后引用**：用 `add()` 的返回值拿对象号，别写 `len(objs)+2` 这种推算，
   加字段时必错。
4. 文本定位用 `BT /F1 <size> Tf 1 0 0 1 <x> <y> Tm <hex> Tj ET`，
   `Tm` 的 y 是**基线**，不是左上角。
5. 折行自己算宽度：CJK 1 em、其他 0.52 em 够用；中文没有空格，必须按字符贪心切，
   英文才按单词切（否则会在单词中间断开）。
6. 页脚页码要等总页数确定后再逐页补写（先渲染正文、再回填）。

### 🔴 中文的坑（这条最容易白干一天）

PDF 规范里可以**引用预定义 CJK 字体**（`/BaseFont /STSong-Light` + `/Encoding /UniGB-UCS2-H`，
正文直接写 UCS-2BE 十六进制串），不用嵌字体文件（嵌一个中文字体动辄 10 MB）。
再加一份**恒等映射的 ToUnicode CMap**，`pypdf` 之类就能正确提取中文。

**但实测**：Edge / Chrome 内置阅读器（PDFium）打开这种 PDF ——
**布局、表格底纹、页脚页码全对，中文全部渲染成乱码**（它不做 Adobe-GB1 字符集回退）。
`pypdf` 能提取出正确中文**不代表浏览器画得出来**。

所以中文 PDF 有三条路，按项目约束挑：

| 方案 | 中文渲染 | 代价 |
|---|---|---|
| **打印就绪 HTML → 浏览器「另存为 PDF」** | ✅ 一定正确（走系统字体） | 需要浏览器参与；不能服务端批量产出文件 |
| 服务端直出 + 预定义 CJK 字体（本文那套） | ❌ Chromium 乱码 / ✅ Acrobat·WPS·Foxit | 零依赖、文字可选可提取，但要向用户说明 |
| 引入 reportlab + 中文字体（子集内嵌） | ✅ 任何阅读器 | 新依赖 + 需要一个字体文件路径（Windows 探测 `simhei.ttf`/`msyh.ttc`，Linux 探测 Noto） |

**推荐做法**：主推「打印版」，同时保留服务端直出 PDF，并在**界面上写明差异**。
不要假装没这个问题 —— 用户点一次「导出 PDF」看到乱码，比没有这个按钮更伤。

### 验证手段（缺一不可）

- 结构 + 编码：`pypdf` 解析 → 页数 > 0、`extract_text()` 能拿到中文、页脚在。
- **真实渲染：用无头浏览器打开 PDF 并截图**（`page.goto(file://…pdf)` 或 `<embed src=…>`），
  肉眼看一眼。这一步才抓得到上面那个乱码问题。

---

## 三、超长内容与流式

- 表格行很多时按「估算行高 + 剩余空间」分页，不要靠 `try/except` 兜。
- 单次导出几百 KB 以内直接 `Response(content=bytes, media_type=...)` 就行，
  不必 `StreamingResponse`。

## 四、下载接口配套（Linux/Windows 都要过）

- 文件名带中文时，`Content-Disposition` **必须 RFC 6266**：
  `attachment; filename="report.xlsx"; filename*=UTF-8''%E4%B8%AD%E6%96%87.xlsx`。
  直接塞中文会被 Starlette 按 **latin-1** 编码并抛 `UnicodeEncodeError`。
- 导出接口若需鉴权，前端**不能用 `<a href>` / `window.open`**（不会带 Bearer Token，必然 401），
  要 `fetch` + `URL.createObjectURL(blob)` 再触发下载。
- 导出的数据源最好先落一份**快照**（JSON），几个出口都读同一份 ——
  否则「同一天导出的两份文件数字不一样」，这种口径漂移最难查。
