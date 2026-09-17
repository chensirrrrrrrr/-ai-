# -*- coding: utf-8 -*-
"""把技术文档 Stage1 的 final_draft.md 转成 Stage2 的「排版就绪 HTML」。

与 `_md2html_audit.py` 的区别：这里要复现 Stage2 模板的完整形态 ——
封面（复用模板原样）、目录（按 md 的二级标题自动生成）、
每个二级标题包一层 `<div class="chapter" id="...">`、
二级标题编号转中文数字（一、二、…）、三级标题编号转（一）（二）…、
表格题注按表头签名取自脚本内的 CAPTIONS 表（未登记的表按所在章节自动编号）。

用法：
    python scripts/tech_doc_md2html.py \
        docs/tech-doc-pipeline/stage1/final_draft.md \
        docs/tech-doc-pipeline/stage2/formatted-留学机构AI智能助手系统技术实现与部署文档.html
"""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path

# 中文数字（1..20）
_CN = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
       "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十"]


def cn(n: int) -> str:
    return _CN[n] if 0 <= n < len(_CN) else str(n)


def inline(text: str) -> str:
    out = html.escape(text, quote=False)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    return out


def norm_sig(cells) -> str:
    """表头签名：去掉所有空白后拼起来（模板里的表头带多余空格，如 `HTTP  状态`）。"""
    return "|".join(re.sub(r"\s+", "", c) for c in cells)


# 表格题注表：键为「表头签名」（去空白后以 | 连接），值为题注。
# 内嵌在脚本里而不是从上一版 HTML 里抓 —— 否则脚本会读到自己上一轮的输出，
# 越跑越脏、且不可重跑。新增表格时在这里补一条即可。
CAPTIONS = {
    "层次|技术选型|版本（实测）": "表 1-1　技术栈与实测版本",
    "层次|组成|职责": "表 2-1　分层架构与职责",
    "变量名|默认值|说明": "表 3-1　环境变量清单",
    "业务域|数据表|说明": "表 4-1　数据表按业务域划分",
    "错误码|HTTP状态|含义|触发场景示例": "表 5-1　错误码定义",
    "角色|可访问Agent白名单|默认Agent": "表 5-2　角色与 Agent 白名单",
    "应用名|类型|端点|职责": "表 5-3　Dify 应用清单",
    "模板ID|用途": "表 5-4　受控查询模板清单",
    "类别|含义|可见角色": "表 5-5　待办类别与可见角色",
    "key|报告|粒度|覆盖范围": "表 5-6　五类业务报告口径",
    "功能组|接口数|主要接口": "表 6-1　接口清单（按功能组）",
    "用户名|密码|角色|说明": "表 7-1　演示账号",
    "模块|覆盖率|说明": "表 8-1　单元测试覆盖率分布",
    "链路|用例数|关键断言": "表 8-2　冒烟测试链路覆盖",
    "编号|现象|根因|修复方式": "表 8-3　开发阶段缺陷修复记录",
    "验收项|标准|实测|结论": "表 8-4　验收结论",
    "参数|取值|位置": "表 B-1　核心参数速查",
}


def split_lead(text: str):
    """把 `**引导句。** 正文...` 拆成 (lead, body)；无引导句则返回 (None, text)。"""
    m = re.match(r"^\*\*(.+?)\*\*\s*(.*)$", text, re.S)
    if m:
        return m.group(1), m.group(2)
    return None, text


def take_cover(template_html: str) -> tuple[str, str]:
    """返回 (head(含 style), cover 段) 原样复用。"""
    head = template_html[: template_html.index("</head>") + len("</head>")]
    start = template_html.index('<section role="cover">')
    end = template_html.index("</section>", start) + len("</section>")
    return head, template_html[start:end]


def convert(md: str) -> tuple[str, list[tuple[str, str]]]:
    lines = md.splitlines()
    out: list[str] = []
    toc: list[tuple[str, str]] = []
    i = 0
    ch_no = 0          # 第几章（1..）
    ch_id = ""         # 当前 chapter 的 div id
    sub_no = 0         # 章内三级标题序号
    tbl_no = 0         # 章内表格序号
    prev_para = ""     # 上一个段落纯文本，用于兜底题注
    appendix = ""

    def close_chapter():
        if ch_id:
            out.append("  </div>")

    while i < len(lines):
        line = lines[i]
        s = line.strip()

        # ---------- 标题 ----------
        m = re.match(r"^## (\d+)\.\s+(.*)$", s)
        m_app = re.match(r"^## 附录\s+([A-Z])：\s*(.*)$", s)
        m_h3 = re.match(r"^### (\d+)\.(\d+)\s+(.*)$", s)
        if s.startswith("# ") and not s.startswith("## "):
            i += 1
            continue
        if m:
            close_chapter()
            ch_no, title = int(m.group(1)), m.group(2).strip()
            heading = f"{cn(ch_no)}、{title}"
            ch_id = f"section-{ch_no}"
            appendix = ""
            toc.append((ch_id, heading))
            out.append(f'  <div class="chapter" id="{ch_id}">')
            out.append(f"    <h2>{inline(heading)}</h2>")
            sub_no = 0
            tbl_no = 0
            prev_para = ""
            i += 1
            continue
        if m_app:
            close_chapter()
            letter, title = m_app.group(1), m_app.group(2).strip()
            heading = f"附录 {letter}：{title}"
            ch_id = f"appendix-{letter.lower()}"
            appendix = letter
            toc.append((ch_id, heading))
            out.append(f'  <div class="chapter" id="{ch_id}">')
            out.append(f"    <h2>{inline(heading)}</h2>")
            sub_no = 0
            tbl_no = 0
            prev_para = ""
            i += 1
            continue
        if m_h3:
            sub_no += 1
            heading = f"（{cn(sub_no)}）{m_h3.group(3).strip()}"
            out.append(f"    <h3>{inline(heading)}</h3>")
            prev_para = ""
            i += 1
            continue

        # ---------- §9 的两个加粗小标题：md 里是纯文本行，模板里是 h3 ----------
        if s in ("**已知限制：**", "**后续演进建议（按优先级）：**"):
            sub_no += 1
            name = "已知限制" if "已知限制" in s else "后续演进建议"
            out.append(f"    <h3>（{cn(sub_no)}）{name}</h3>")
            prev_para = ""
            i += 1
            continue

        # ---------- 代码围栏 ----------
        if s.startswith("```"):
            i += 1
            buf = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append("    <pre>" + html.escape("\n".join(buf), quote=False) + "</pre>")
            prev_para = ""
            continue

        # ---------- 表格 ----------
        if s.startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            header = [c.strip() for c in s.strip("|").split("|")]
            rows = []
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            tbl_no += 1
            cap = CAPTIONS.get(norm_sig(header))
            if not cap:
                prefix = f"表 {appendix}-{tbl_no}" if appendix else f"表 {ch_no}-{tbl_no}"
                subject = re.split(r"[：:，,。（(]", prev_para)[0][:18] if prev_para else "内容一览"
                cap = f"{prefix}　{subject}"
            out.append("    <table>")
            out.append(f"      <caption>{inline(cap)}</caption>")
            out.append("      <thead>")
            out.append("        <tr>" + "".join(f"<th>{inline(c)}</th>" for c in header) + "</tr>")
            out.append("      </thead>")
            out.append("      <tbody>")
            for r in rows:
                out.append("        <tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
            out.append("      </tbody>")
            out.append("    </table>")
            prev_para = ""
            continue

        # ---------- 引用 / 列表 / 分隔线 ----------
        if s == "---":
            out.append("    <hr>")
        elif not s:
            pass
        elif s.startswith("> "):
            out.append(f"    <blockquote><p>{inline(s[2:])}</p></blockquote>")
        elif s.startswith(("- ", "* ")):
            items = []
            while i < len(lines) and lines[i].strip().startswith(("- ", "* ")):
                items.append(f"<li>{inline(lines[i].strip()[2:])}</li>")
                i += 1
            out.append("    <ul>" + "".join(items) + "</ul>")
            prev_para = ""
            continue
        elif re.match(r"^\d+\.\s", s):
            items = []
            while i < len(lines) and re.match(r"^\d+\.\s", lines[i].strip()):
                items.append(f"<li>{inline(re.sub(r'^\d+\.\s', '', lines[i].strip()))}</li>")
                i += 1
            out.append("    <ol>" + "".join(items) + "</ol>")
            prev_para = ""
            continue
        else:
            lead, body = split_lead(s)
            if lead:
                out.append(f"    <p><strong>{inline(lead)}</strong> {inline(body)}</p>")
            else:
                out.append(f"    <p>{inline(s)}</p>")
            prev_para = s
        i += 1

    close_chapter()
    return "\n".join(out), toc


def main() -> None:
    md_path, html_path = Path(sys.argv[1]), Path(sys.argv[2])
    md = md_path.read_text(encoding="utf-8")
    template = html_path.read_text(encoding="utf-8")

    head, cover = take_cover(template)
    body, toc = convert(md)

    toc_html = "\n".join(f"      <li>{html.escape(t)}</li>" for _, t in toc)
    page = (
        head
        + "\n<body>\n\n"
        + cover
        + '\n\n<section role="body" data-page-restart="1">\n\n'
        + '  <nav class="doc-toc" aria-label="文档目录">\n'
        + '    <p class="toc-title">目录</p>\n'
        + '    <ol class="toc-list">\n'
        + toc_html
        + "\n    </ol>\n  </nav>\n\n"
        + body
        + "\n\n</section>\n</body>\n</html>\n"
    )
    html_path.write_text(page, encoding="utf-8")
    print(f"OK  {html_path}  ({len(page)} bytes)  章节 {len(toc)}  题注表 {len(CAPTIONS)} 条")


if __name__ == "__main__":
    main()
