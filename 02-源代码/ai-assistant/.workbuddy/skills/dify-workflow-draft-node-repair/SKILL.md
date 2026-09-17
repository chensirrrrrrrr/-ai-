---
name: dify-workflow-draft-node-repair
description: 诊断并修复「Dify 工作流在网页编辑器里节点显示成细白条 / 点不进去 / 检查清单误报『必须添加直接回复节点』」。真因是用 API 建图时把节点**顶层 `type`** 写成了 `answer`/`llm` 等块类型，而 Dify 画布只注册了一个 ReactFlow 组件（`type="custom"`，真正种类在 `data.type`），于是 ReactFlow 退化成内置 default 节点渲染成 150x21 白条。提供判据（顶层 type 分布 / 150x21 尺寸 / 检查清单报错组合）、自包含标准库修复脚本（改 type + 归一化尺寸，带 draft hash 乐观锁），以及「后端只认 data.type，所以已发布版本一直正常、纯前端问题」这个关键结论。当用户说「Dify 节点是白条」「工作流画布点不动」「提示必须添加直接回复节点」时使用。
agent_created: true
---

# Dify 工作流节点显示成细白条 —— 诊断与修复

## 症状（三个一起出现就是本问题）

1. 网页编辑器画布上节点是**细白色横条**，几乎看不见，也点不进去（无法编辑）；
2. draft 里 `nodes[].width/height` 全是 **150x21**；
3. 右侧「检查清单」报 **「必须添加直接回复节点」**（`workflow.common.needAdd` + `blocks.answer`），
   **哪怕图里明明有多个 answer 节点**。

与此同时 **API 跑这个应用一切正常**（RAG 命中、HTTP 取数、分支路由都对）。

## 一句话根因

**节点的顶层 `type` 必须是 `"custom"`。**

Dify 画布给 ReactFlow 的 `nodeTypes` 只注册了一个组件，key 是 `"custom"`；
真正的节点种类（start / llm / answer / http-request / question-classifier / knowledge-retrieval …）
一律放在 **`data.type`** 里。

前端有这段兜底（1.10.0 编译产物 `web/app/components/workflow` 的 `flowToCanvas`）：

```js
return a.map(e => { e.type || (e.type = l.hB); ... })   // l.hB === "custom"
```

**只在顶层 `type` 缺失时才补 `"custom"`。** 用 API 建图时若显式写了 `"type": "answer"`，
前端会原样保留 → ReactFlow 在 `nodeTypes` 里查不到 `answer` → 退化成内置 **default 节点**。

ReactFlow v11 的 default 节点 CSS：

```css
.react-flow__node-default{ padding:10px; width:150px; font-size:12px;
  text-align:center; border:1px solid #1a192b; background-color:#fff; ... }
```

空节点量出来正好 **150x21 的白色横条**，与前端的自动保存值完全吻合。

次生症状的原理：检查清单只统计 `type === "custom"` 的节点

```js
let i = e.filter(e => e.type === c.hB);                    // c.hB === "custom"
Object.keys(p).filter(n => p[n].metaData.isRequired).forEach(n => {
  i.find(t => t.data.type === n) || r.push({ ...needAdd... })
})
```

`i` 为空 ⇒ `answer`（`metaData.isRequired === true`）找不到 ⇒ 误报。
注意只会报 **answer** 一条：start 的检查走的是另一条分支（用的是全量 `e`，所以 start 不报错），
**「只报 answer 一条」本身就是本问题的指纹**。

## 关键结论：为什么已发布版本一直正常

**后端执行只读 `data.type`，完全不看顶层 `type`。** 所以：

- 已发布版本照跑不误（我们实测 6 个分支全部返回真实数据）；
- 但**编辑器废了**，用户改不了；
- 点「发布」会把 `type: "answer"` 一起发出去 —— 依然不影响运行。

⇒ 这是**纯前端 + 编辑器可用性**问题，别去动后端、别去动已发布版本。

## 第一步：先证伪，别急着改

### 判据 A（决定性）：看顶层 `type` 分布

```bash
python - <<'PY'
import wf_lib; D = wf_lib.Dify()          # 或直接用 skill 里的脚本 dry-run
_, d = D.get_draft('<app_id>')
import collections
print('top-level type:', dict(collections.Counter(n['type'] for n in d['graph']['nodes'])))
print('data.type     :', dict(collections.Counter(n['data']['type'] for n in d['graph']['nodes'])))
PY
```

**顶层 type 不是清一色 `custom` ⇒ 确诊。**（data.type 该是什么还是什么，两者不一致就是病征。）

### 判据 B：仓库/脚本自查

```bash
grep -rn '"type": *ntype\|type.*=.*ntype' --include=*.py <你的建图脚本目录>
```

建图函数里如果有 `{"id": nid, "type": ntype, ...}`，就是它。

### 判据 C（辅助）：尺寸分布

```bash
docker exec <db容器> psql -U postgres -d dify -A -F'|' -c "
select a.name, w.version,
       (select string_agg(distinct (nd->>'width')||'x'||(nd->>'height'), ',')
          from jsonb_array_elements(((w.graph)::jsonb)->'nodes') nd) as dims
from workflows w join apps a on a.id = w.app_id
where a.mode in ('workflow','advanced-chat');"
```

draft 一行出现 `150x21` ⇒ 前端已按 default 节点测量并回写。
> ⚠️ `workflows.graph` 是 **TEXT** 列，必须 `::jsonb` 转型。

### ⛔ 曾经的错误诊断（别重蹈）

早期版本把根因写成「前端在自定义节点渲染前抢先测量尺寸」，并建议**核对顶层 `type` 是否仍是
`start`/`llm`** —— **完全反了**：顶层 `type` 恰恰应该是 `custom`。照那个思路修只会反复
把 `width/height` 改回 244x90，一开编辑器又塌，永远治不好。

## 第二步：修复

用 `scripts/dify_draft_dims.py`（自包含，只用标准库）：

```bash
python scripts/dify_draft_dims.py --base http://127.0.0.1 \
    --email <控制台邮箱> --password '<控制台密码>'                        # 只检测
python scripts/dify_draft_dims.py --base http://127.0.0.1 \
    --email <..> --password '<..>' --apply --yes                        # 全部 workflow 应用
python scripts/dify_draft_dims.py --base http://127.0.0.1 \
    --email <..> --password '<..>' --app <app_id或应用名> --apply --yes   # 只修一个
```

脚本行为：

- 顶层 `type` 统一改成 `"custom"`；
- 尺寸按 **`data.type`** 归一化（`start`/`end`/`answer` → 244x90，其余 → 244x98）；
- **`data` 一个字段都不碰**（prompt / model / variables / 连线原样保留）；
- 写回带刚读到的 `hash` 做乐观锁；
- 默认 dry-run，`--apply` 才写。

### 同时修掉建图脚本（治本）

```python
return {"id": nid, "type": "custom", "data": d, ...}   # 不是 ntype！
```

## ⚠️ 坑

1. **改完让用户 `Ctrl+Shift+R` 强制刷新**编辑器页。页面里若还留着旧的（`type: "answer"`）
   图，它下一次自动保存会把你的修复覆盖回去。
2. 修好后**不必**再手动纠结 `width/height`：顶层 type 一旦正确，ReactFlow 会渲染真节点、
   量出真尺寸再回写，形成一个稳定状态。
3. `edges[].type` 本来就应该且必须是 `"custom"`（`edgeTypes.custom`），别搞混。
4. `custom-iteration-start` / `custom-loop-start` 是迭代/循环容器专用的另外两个
   ReactFlow key，只在容器内部用。

## 内网自建 Dify 的两个前置

- 调本机 `http://127.0.0.1` **必须绕开系统代理**：Python 里 `ProxyHandler({})`，
  命令行加 `NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost`。
- 控制台是 cookie 登录，写操作要带 `X-CSRF-Token`（取自 `csrf_token` cookie）。
- 想确认前端行为，可直接读容器里的编译产物（不用翻 GitHub）：
  `docker exec <web容器> grep -rho '.\{0,160\}type:\"custom\".\{0,160\}' /app/web/.next/static/chunks/*.js`
  配合 `node -e` 打印上下文最快（容器里有 node）。
