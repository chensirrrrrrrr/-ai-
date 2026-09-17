---
name: api-implementation-audit
description: 用「交付文档 vs 真实代码」做三查——① 功能是否都实现了 ② 代码是否有冗余/死代码 ③ 各功能有没有 bug。手法：app.openapi() 取真实路由清单逐条比对交付文档的接口表；AST 静态扫描死 schema/未用导入/未调用函数；pytest+覆盖率定位未测分支。当用户拿着某份需求/技术文档问「这些功能都实现了吗、代码有没有冗余、测一遍有没有 bug」，或需要在交付/答辩前做一次实现侧自证时使用。
agent_created: true
---

# 交付文档 vs 代码：实现完整性 / 冗余 / 缺陷 三查

**适用**：用户给一份 docx/md 交付文档，要求核验实现情况。产出是一份可复核的报告，不是口头结论。

**核心原则**：三查必须各自有**可复现的机器证据**，不能靠读代码"感觉"：
- 完整性 → 接口清单逐条 diff（**有无缺失**）
- 冗余 → AST 扫描（**哪些是死代码**）
- 缺陷 → 三层实测 + 覆盖率（**哪些分支没人测**）

---

## 一、查功能完整性：openapi 清单 vs 文档接口表

### 1.1 取真实路由清单（唯一可信来源）

```python
from app.main import app
spec = app.openapi()["paths"]          # 不要手数装饰器
for p, item in sorted(spec.items()):
    ms = [m.upper() for m in item if m.lower() in ("get","post","put","patch","delete")]
    print(p, ms)
```

按 tag 分组统计，同时打印 `op` 总数（一条路径可能挂多个方法）：
**文档说"N 个接口"时，先确认它数的是「路径」还是「操作」**，两者常差 10%+。

### 1.2 ⚠️ 比对前必须做路径参数归一

文档写 `/leads/{id}`、代码是 `/leads/{lead_id}` —— **字面比对会把同一接口误判成「文档有代码缺」**。

```python
import re
norm = lambda s: re.sub(r"\{[^}]+\}", "{}", s)   # 参数名统一成 {}
docset  = {(m, norm(p)) for m, p in 文档解析结果}
realset = {(m, norm(p.replace("/api/v1", ""))) for p, item in spec.items()
           for m in item if m.lower() in ("get","post","put","patch","delete")}
print("文档有代码缺:", sorted(docset - realset))    # 应为你关心的「功能缺失」
print("代码有文档未登记:", sorted(realset - docset))  # 通常是文档滞后
```

**两个方向的结论完全不同**：
- `doc - real` 非空 ⇒ **真缺失**（功能没实现），要单独列。
- `real - doc` 非空 ⇒ 一般只是**文档滞后**（后加能力没回填），动作项是「更新文档」，不是「补代码」。

还要注意**文档写法不准**（如写 `PATCH /alerts`，真路由是 `PATCH /alerts/{alert_id}`）—— 报「存在，仅写法不准」，别报成缺失。

### 1.3 解析 docx 的接口表

```python
import docx
d = docx.Document(r"...\xxx.docx")
# 找接口表：含 "/api" 或 "/internal" 最多的那张
best = max(d.tables, key=lambda t: "\n".join("|".join(c.text for c in r.cells) for r in t.rows)
                                          .count("/api") + "\n".join("|".join(c.text for c in r.cells) for r in t.rows).count("/internal"))
```
⚠️ **不要在项目根目录（含同名干扰模块的目录）里 `import docx`** —— 会报
`module 'docx' has no attribute 'seek'`。先 `cd` 到一个干净目录（如 `C:\Users\Public\...`）再跑，docx 用绝对路径。

---

## 二、查冗余：AST 静态扫描

| 查什么 | 怎么查 | 判据 |
| --- | --- | --- |
| **响应模型死代码** | `grep -rn "response_model" app/ \| wc -l` | `=0` ⇒ 所有 `*Out` Pydantic 类都没被用（响应走自研信封时极常见） |
| **未引用 schema 类** | 见下方脚本 | 类名在 api/crud/services+tests+scripts 里**零出现** |
| **未使用导入** | AST：收集 Import/ImportFrom 名 → 对比 Name/Attribute/字符串注解 | 只出现 1 次（即 import 行自己）= 未用 |
| **未被调用函数** | 模块级 def，排除 `@router.` 装饰与 `test_`，全文出现次数 ≤1 | ≤1 ⇒ 无调用方 |
| **散落临时物** | 根目录 `*.py`、`_lab/` 之类 | 无任何 import 引用即为临时 |

未引用 schema 扫描 —— ⚠️ **必须算传递可达闭包，不能只排除自身文件**：

```python
import ast, os, re
files = [os.path.join(dp, f) for sub in ("app","tests","scripts")
         for dp,_,fns in os.walk(sub) if "__pycache__" not in dp for f in fns if f.endswith(".py")]
external = "\n".join(open(f, encoding="utf-8", errors="ignore").read()
                     for f in files if not f.endswith("schemas.py"))
self_src = open("app/schemas.py", encoding="utf-8").read()
tree = ast.parse(self_src)
classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]

# ① roots = 被「本文件之外」引用的类
def used(name, src):
    return re.search(r"\b"+re.escape(name)+r"\b", src) is not None
alive = {c for c in classes if used(c, external)}

# ② 沿 schemas.py 内部引用做闭包扩散（这才是关键）
while True:
    new = {c for c in classes if c not in alive
           and any(re.search(r"\b"+re.escape(c)+r"\b", body)
                   for other in alive
                   for body in [ast.get_source_segment(self_src, n)
                                for n in tree.body if isinstance(n, ast.ClassDef) and n.name == other]
                   if body)}
    if not new: break
    alive |= new

dead = [c for c in classes if c not in alive]
print(len(classes), "类，", len(dead), "真死:", dead)
```

> 🔴 **实测教训**：只做「排除自身文件」的朴素版，会把**只被同文件内其他类引用**的类误判为死代码。
> 本项目朴素版报 **24** 个，算传递可达后真死只有 **20** ——
> `RuleItem`/`RuleProduct`（`RuleImportRequest.products` ↔ `RuleProduct.rules`）、
> `DeadlineItem`/`ProgressItem`（`DeadlineImport.items` / `ProgressImport.items`）都是活的。
> **多报 = 误删有引用的类 = 直接炸运行期**，所以判死前一定跑可达闭包，并**打印存活路径**人工抽查几个。

**注意别误报**：
- `from . import models  # noqa: F401` 是**有意**的表注册，不是死代码。
- `from __future__ import annotations` 在 Python ≥3.11 非必需，可留可去，**不算冗余缺陷**（最多提一句）。
- 跨文件同名小函数（`build`/`to_dict`/`db`/`main`）多为各文件局部工具或测试夹具，**不是重复实现**，别当冗余报。

---

## 三、查缺陷：三层实测 + 覆盖率

必须**三层都跑**，层与层抓的东西不同（详见技能 `http-smoke-baseline`、`zero-build-frontend-verify`）：

```bash
# 1) 单元/集成 + 覆盖率（覆盖率直接指出「哪些功能没人测」）
python -m pytest -q --cov=app --cov-report=term-missing:skip-covered
# 2) HTTP 冒烟（真实服务）
python scripts/smoke_test.py --base http://127.0.0.1:8010
# 3) 真实浏览器 E2E（零构建前端）
python scripts/serve_frontend.py --host 127.0.0.1 --port 8020 --api-base http://127.0.0.1:8010   # 后台起
WB_NODE_WORKSPACE=<...>/binaries/node/workspace node scripts/frontend_e2e.mjs
```

**看覆盖率要抓「低覆盖模块」而不是总分**：把 `Miss` 多的模块按覆盖率升序列出，
它们就是「有代码但没测到」的嫌疑区（定时任务分支、DB 迁移分支、写工具的预览/确认分支最常漏）。
若某低覆盖行正好落在第二章扫出的**死函数**上 ⇒ 交叉印证「该函数确实没人调」。

收尾：`taskkill /PID <pid> /F /T`（PID 从 `netstat -ano | tr -d '\r'` 取；**不是** `//PID`）。
调本机一律带 `NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost`。

**报"无 bug"要限定范围**：说清"在已覆盖的链路上未发现功能性缺陷"，而不是"没有 bug"。

---

## 四、报告骨架（照抄）

```
一、功能是否都实现了？—— 是/否
   1.1 文档登记接口 → 实现率（doc-real / real-doc 两个数字）
   1.2 真实 API 分组分布（文档 vs 真实，差额列）
   1.3 文档未登记的 N 个已实现接口（按能力归组，列出）
   1.4 需求层面（如有需求规格书，引用既有查验报告）
二、代码是否有冗余？—— 有，逐项列（死 schema / 未用导入 / 未调用函数 / 临时物）+ 每项"可安全清理"判断
三、测试各功能是否有 bug？—— 三层结果 + 覆盖率 + 低覆盖模块表 + 基线对照
四、结论与建议动作：表格 [事项 | 优先级 | 建议 (| 落地)]
五、整改落地明细（若用户确认执行）：先备份 → 逐项改动 → 回归数字 → 基线文档同步清单
```

**最后一句务必声明**：本次查验是否改动过业务代码（通常"仅新增报告文件"）。

**报告数字要分三层口径**，别混成一个数：**查验时快照 / 整改后实测 / 原基线**。
查验时写的数字**不要事后改写**（保留快照便于对照），整改结果另开一节。

> 整改若获用户确认，闭环动作清单（本项目实测有效）：
> ① 备份 zip（无 git 时的唯一安全网）② 回填文档接口清单（md/docx/HTML 三处同步）
> ③ 删死代码（含连带失效的 `ConfigDict` 之类导入）④ 死函数**优先"补接线"而非删**（若它是有意义的缺失能力，如写工具的 `cancel`）
> ⑤ 临时物**移出归档**而非直删 ⑥ 对低覆盖模块补单测 ⑦ 全量回归 ⑧ 同步所有基线文档的数字。

---

## 五、坑位清单

1. 路径参数**必须归一**再 diff，否则误报一堆"缺失"。
2. `response_model=0` 是**提示**不是结论 —— 先看是不是自研响应信封，再决定是"清理死类"还是"补响应校验"。
3. 解析 docx 要换到干净目录，避开同名模块干扰。
4. 就绪探针路径各项目不同，**探测一下**再写进报告（常见 `/api/v1/ready` ≠ `/api/v1/health/ready`）。
5. E2E 的 console 错误/失败请求数要**逐条看是否用例自身预期**（越权 403、故意坏端口都是预期），别直接当缺陷。
6. "文档有代码缺 = 0" 才敢下"功能无缺失"的结论；只要有 1 条非写法问题，就必须单列并定级。
7. 🔴 **判死代码必须算传递可达闭包**（见 §二 脚本）：「只排除自身文件」的朴素版会多报 —— 本项目 24 vs 真死 20，
   多报的就差 `RuleItem/RuleProduct/DeadlineItem/ProgressItem` 这 4 个只被同文件引用的类。误删即炸运行期。
8. `pytest.ini` 里常已内置 `-q`；**命令行再加 `-q` 会把「N passed」汇总行吞掉**，想看数字就别加，或用 `-rA`。
9. 整改执行前**先备份**（无 git 的项目尤其）：打 zip 并以「备份 → 改 → 全量回归 → 同步基线文档」为固定节奏。
