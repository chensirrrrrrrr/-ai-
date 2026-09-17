"""把查验报告 markdown 重新转成自包含 HTML（复用既有 HTML 的 <style>）。

用法: python scripts/_md2html_audit.py reports/需求完成情况查验报告
"""
import html
import re
import sys
from pathlib import Path


def take_style(template_html: str) -> str:
    m = re.search(r"<style>.*?</style>", template_html, re.S)
    if not m:
        raise SystemExit("template has no <style> block")
    return m.group(0)


def inline(text: str) -> str:
    out = html.escape(text, quote=False)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    return out


def convert(md: str) -> str:
    lines = md.splitlines()
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("|") and i + 1 < len(lines) and re.match(
            r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]
        ):
            header = [c.strip() for c in stripped.strip("|").split("|")]
            out.append("<table>")
            out.append(
                "<thead><tr>"
                + "".join(f"<th>{inline(c)}</th>" for c in header)
                + "</tr></thead>"
            )
            out.append("<tbody>")
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                out.append(
                    "<tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells) + "</tr>"
                )
                i += 1
            out.append("</tbody></table>")
            continue

        if stripped == "---":
            out.append("<hr>")
        elif not stripped:
            pass
        elif stripped.startswith("#### "):
            out.append(f"<h4>{inline(stripped[5:])}</h4>")
        elif stripped.startswith("### "):
            out.append(f"<h3>{inline(stripped[4:])}</h3>")
        elif stripped.startswith("## "):
            out.append(f"<h2>{inline(stripped[3:])}</h2>")
        elif stripped.startswith("# "):
            out.append(f"<h1>{inline(stripped[2:])}</h1>")
        elif stripped.startswith("> "):
            out.append(f"<blockquote><p>{inline(stripped[2:])}</p></blockquote>")
        elif stripped.startswith("- "):
            items, j = [], i
            while j < len(lines) and lines[j].strip().startswith("- "):
                items.append(f"<li>{inline(lines[j].strip()[2:])}</li>")
                j += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            i = j
            continue
        elif re.match(r"^\d+\.\s", stripped):
            items, j = [], i
            while j < len(lines) and re.match(r"^\d+\.\s", lines[j].strip()):
                items.append(
                    f"<li>{inline(re.sub(r'^\d+\.\s', '', lines[j].strip()))}</li>"
                )
                j += 1
            out.append("<ol>" + "".join(items) + "</ol>")
            i = j
            continue
        else:
            out.append(f"<p>{inline(stripped)}</p>")
        i += 1
    return "\n".join(out)


def main() -> None:
    stem = Path(sys.argv[1])
    md_path = stem.with_suffix(".md")
    html_path = stem.with_suffix(".html")
    md = md_path.read_text(encoding="utf-8")
    style = take_style(html_path.read_text(encoding="utf-8"))
    title = md.splitlines()[0].lstrip("# ").strip()
    page = (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f"<title>{html.escape(title)}</title>\n{style}\n</head>\n<body>\n<main>\n"
        + convert(md)
        + "\n</main>\n</body>\n</html>\n"
    )
    html_path.write_text(page, encoding="utf-8")
    print(f"OK  {html_path}  ({len(page)} bytes)")


if __name__ == "__main__":
    main()
