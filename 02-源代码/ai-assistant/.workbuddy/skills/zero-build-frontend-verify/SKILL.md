---
name: zero-build-frontend-verify
description: 在 Windows 上验证「零构建前端」（原生 ES Module + 手写 CSS，无 npm/打包器）是否真的能跑。做三层检查：JS 语法、import/export 一致性、以及用 puppeteer-core 驱动本机 Edge 的真实浏览器端到端并逐页截图。当需要交付单文件/无构建前端、或怀疑前端白屏、或要做「前端也测一遍」的验收时使用。
agent_created: true
---

# 零构建前端的本地验证

适用对象：直接开浏览器就跑的前端（`index.html` + `js/*.js` 的 ES Module + 手写 CSS），
没有 `package.json` / 打包器 / node_modules。这类项目最典型的翻车方式不是逻辑错，
而是**一个不存在的具名导入**——浏览器里表现为整页白屏，错误只在控制台可见。

所以验证分三层，从便宜到贵，任何一层挂了就不用往下走。

---

## 一、三层检查

### L1 语法：`node --check`（但要先改名成 .mjs）

```bash
# ⚠️ 不能直接 --check 带 import 的 .js —— node 会按 CommonJS 解析并报错
for f in $(find frontend -name '*.js'); do
  cp "$f" "/tmp/chk/$(echo $f | tr '/' '_').mjs"
  node --check "/tmp/chk/$(echo $f | tr '/' '_').mjs"
done
```

Windows 上直接用 `scripts/check_syntax.ps1`（本 skill 自带）。

### L2 导入一致性：正则比对 export / import

跑 `scripts/check_frontend_imports.py`。它扫描 `frontend/**/*.js`，
收集每份文件的导出名（`export function/const/class`、`export {a, b}`、`export default`），
再逐条校验 `import { x } from './y.js'` 里的 `x` 是否真的存在、相对路径是否解析得到。

退出码 0 = 一致。这是**性价比最高的一层**：不装浏览器、秒级、专抓白屏级错误。

### L3 真实浏览器：puppeteer-core + 本机 Edge

跑 `scripts/frontend_e2e.mjs`（需要改成你项目的选择器）。它做：
打开页面 → 登录 → 逐个导航项点一遍并检查有没有「页面加载失败」→ 跑一轮核心交互 →
收集 `console` 错误 / `pageerror` / 失败请求（status ≥ 400）→ 逐页截图 → 写 JSON 报告。
全通过时退出码 0。

关键断言设计（照抄这套思路）：
- **每个视图都比对 `#view` 内的文字长度**：太短或含「加载失败」字样就算挂，能抓出静默失败。
  ⚠️ **实测吃过亏**：视图内部的 `try/catch` + `reportError()` 只弹一个 toast，
  既不抛 `pageerror` 也不进 `console.error` —— 所以「0 console 错误」**不代表**页面渲染成功。
  两个动作缺一不可：① 失败文案要**把视图内的说法也算进去**（本项目视图用 `emptyBox('加载失败')`、
  路由层用「页面加载失败」，只查后者等于没查）；② 再加一条**空占位计数**断言
  `document.querySelectorAll('#view .empty').length === 0`，并为核心卡片补计数断言
  （列表行数 / 阶段数 / 联系人卡片数），否则「某个卡片挂了」永远走不到红。
  最后**一定要肉眼看一眼截图** —— 上面这个 bug 就是靠看截图抓出来的。
- **收集 `pageerror`**（未捕获异常）比 `console.error` 重要，前者一定是 bug。
- **允许「故意触发」的错误**：越权 403、坏端口 `ERR_UNSAFE_PORT` 这类应当白名单排除，否则报告永远不干净。
- **别只等元素数量，要等内容**。`querySelectorAll('.msg').length >= 2` 会在请求还没回来时就成立（本地那条马上就渲染出来了）。要 `waitForFunction` 检查最后一条气泡的 `textContent.length > 8 && !startsWith('【')`。
- **首步断言的元素必须就是「未登录时的默认可见元素」**。这条最容易腐坏：一旦新增/调整了入口页
  （例如在登录屏之前加一个访客落地页），原来那句 `waitForSelector('#login-screen:not([hidden])')`
  会直接超时，而**报错信息只是「timeout」，看不出是入口变了**。
  改动默认入口时，同一次改动里必须同步：E2E 首步选择器 → 到登录屏的过渡动作（`click('[data-landing="login"]')`）→
  断言「旧入口已隐藏」。
- **每个新页面都要有「内容渲染完整」的计数断言**，而不是只截个图：
  例如 `#capability .landing-card` 应为 6、`#faq details` 应为 4、`[data-landing="login"]` 至少 1 个。
  计数断言能同时抓「模板循环没跑」和「后端字段改名」，截图抓不到。
- **计数断言优先写成「派生关系」而不是魔法数字**。表格里每行都该有两个按钮时，写
  `detailBtns === rows && reviewBtns === rows`，比写 `detailBtns === 11` 强得多：
  前者抓「某个按钮的钩子改名漏改了一处」（实测 11 行里 11 个复核按钮、**0 个详情按钮**，
  就是靠这条抓出来的），后者只会随数据条数一起飘。
  另一条同源经验：**给元素打 `data-*` 钩子时，钩子名要在整个视图里唯一且语义化**
  （列表用 `data-sdetail`、客户行用 `data-detail`），否则一个 `querySelectorAll('[data-detail]')`
  会同时绑到两种语义不同的按钮上，弹出一层套一层的弹窗。
- **截图编号与断言顺序保持一一对应**（`01-landing` / `02-login` / `03-console-chat` / `04-view-*` …）。
  改名后记得把**旧编号的残留 png 归档走**（`Move-Item` 到 `_lab/`），否则交付目录里新旧两套并存，
  报告里列的文件名对不上实际图片。

---

## 二、把「起服 → E2E → 收服」塞进一次工具调用

**受控/沙箱环境里，任何被派生出去的进程都会在本次工具调用结束时被整棵回收。**
实测：用 `DETACHED_PROCESS | CREATE_NO_WINDOW`、`CREATE_NEW_CONSOLE`（`cmd /k`）、
`CREATE_NEW_CONSOLE | CREATE_BREAKAWAY_FROM_JOB` 三种方式各起一个「写标记文件后 sleep 90s」
的子进程，父进程退出后**三个都没写出标记文件**——即被立刻杀掉。

后果：不要把验证拆成「先起服」「再跑 E2E」两次调用，第二次一定连不上。
正确做法是一个 Python 编排脚本，在同一个进程生命周期里做完四件事：

```python
procs = [Popen(backend, stdout=open(log,'wb'), stderr=STDOUT),
         Popen(frontend, ...)]
wait(lambda: health_ok(port))          # 探活
run([node, "scripts/frontend_e2e.mjs"], env={...})   # 跑 E2E
finally: for p in procs: p.terminate(); p.wait()      # 收服
```

好处：不依赖任何「窗口会不会被回收」的假设，CI 里也能原样跑。
产品代码里同时也值得留一个 `--selfcheck`（起服→体检→收服）入口，就是这个模式。

---

## 三、让前端地址跟着端口走（不然换端口必崩）

E2E 和真人使用都怕「后端换了端口，前端还写死老地址」。别让前端硬编码，加一层注入：

1. `frontend/runtime-config.js`（真实存在的占位文件，内容是
   `window.__UAS_API_BASE__ = window.__UAS_API_BASE__ || '';`）——
   **必须有这个文件**，否则用第三方静态服务器托管时 `<script src>` 会 404，
   给 E2E 的「无 console 错误」断言添乱。
2. `index.html` 在 module 脚本**之前**加 `<script src="./runtime-config.js"></script>`。
3. 静态服务器拦截 `/runtime-config.js` 动态吐真实地址（`--api-base` 传入）。
4. 前端取值优先级：`localStorage`（用户手填）> `window.__UAS_API_BASE__` > 代码默认值。

这样 launcher 换端口不用改任何前端代码，E2E 也能用 `FRONT_URL` / `BACK_URL` 环境变量对齐。

> 反例：如果只写 `<script src="./runtime-config.js">` 而不放真实文件，
> 换成 `python -m http.server` 托管时必然 404。

---

## 四、Windows 环境坑（都踩过）

| 坑 | 现象 | 解法 |
|---|---|---|
| **ESM 不认 `NODE_PATH`** | `import 'puppeteer-core'` 报 `ERR_MODULE_NOT_FOUND` | 脚本放 node 工作区跑，或用 `createRequire(<base>/noop.js).resolve('puppeteer-core')` 逐候选目录探测后 `import(pathToFileURL(resolved).href)`。`frontend_e2e.mjs` 已实现 |
| `--user-data-dir` 放项目里 | 留下几十 MB 的 Edge profile，还带 `Edge Sidebar/...` 残留，中文路径下删不掉 | 放 `fs.mkdtempSync(path.join(os.tmpdir(), 'fe-e2e-'))` |
| **`Remove-Item -Recurse -Force` 删不干净** | 含中文的路径下会「删空内容但留下空目录壳」，不报错、`Test-Path` 仍为 True，再删也没用 | 用 .NET 直删：`[System.IO.Directory]::Delete($path, $true)` |
| 无头 Edge 的 `prefers-color-scheme` | 解析为 **dark**，与预期不符 | 别假设默认是浅色（下面那行给了双主题的正确测法） |
| PowerShell 工具不返回 stdout | 看不到任何输出 | 一律 `*> file` 落盘再 Read |
| PowerShell `*>` 重定向是 UTF-16/GBK | Read 报 `Cannot display content of binary file` | 用 python 依次尝试 `utf-8 / utf-16 / gbk` 解码后另存成 utf-8 再读 |
| 🔴 **同一个日志文件混用多种落盘方式** | `Set-Content -Encoding utf8` + `Out-File -Encoding utf8` + `*>` 都会往同一文件追加，结果是 **utf-8 与 utf-16 交错的字节流**，两种编码都解不出来（`errors='replace'` 后只剩乱码），日志等于白留 | 一次调用里**只用一种**方式写同一个文件；跨调用追加时也保持一致。**更稳的做法：不要读 stdout，去读脚本自己产出的结构化报告** —— E2E 读 `reports/frontend_e2e_report.json`（有 `passed/total/steps[].ok/detail`），冒烟读 `reports/smoke_report.md`，比解析控制台输出可靠得多 |
| 断言「导航项数」写死 | 加一个业务视图后 E2E 断言失败 | 用 `views.length >= 6` 这类下界断言；E2E 的 `passed/total` 总数会随视图数自然增长（26 → 27），README/报告里的数字要同一次改动里一起更新 |
| 前端用 `file://` 打开 | ES Module 被同源策略拦，白屏 | 必须用 HTTP 起静态服务。`scripts/serve_frontend.py` 顺手补 `.js` 的 MIME 并关缓存 |
| 🔴 同一文件多处 `Edit` 并发 | 返回值全是 `Successfully edited`，实际只落了 1 处，其余静默丢失 | 改同一文件**串行逐次**调用，或合成**一个**覆盖整段的大 Edit；改完必须回读确认 |
| 🔴 同一个标识符散落在多个 render 分支 | 把 `data-detail` 改名成 `data-sdetail` 时只改中了一处（同文件另一次 Edit 静默丢了），列表里「详情」按钮全部失效、且**不报任何错**（只是点了没反应） | 改名类改动做完立刻 `Grep` **旧名**，确认剩余命中都确实该保留（本例剩下 2 处属客户行，是对的）；只靠「两个 Edit 都说成功」必翻车 |
| 无头 Edge 只看首屏 | 长页面（落地页/官网）首屏截图看不到后面的卡片、FAQ | 视觉抽查用 `page.screenshot({ fullPage: true })`；再补一个 420px 窄屏截图并断言 `scrollWidth - clientWidth === 0`（无横向溢出） |
| 想用「预置 localStorage」测双主题 | `evaluateOnNewDocument` 在**每次导航/刷新时都会重跑**，把主题又覆盖回去 → 深色测不出来 | 别用注入测主题；**点页面上的主题切换按钮**再断言 `data-theme` 变了，顺带把按钮可用性也测了 |

---

## 五、静态服务器

`scripts/serve_frontend.py`：给 `python -m http.server` 包一层，补 `.js`/`.mjs` 的
`text/javascript`、加 `Cache-Control: no-store`（本地联调改完刷新就生效）、
`ThreadingTCPServer + allow_reuse_address`。用法：

```bash
python scripts/serve_frontend.py --host 127.0.0.1 --port 8020
```

## 六、前后端分离的配合点

- 前端**不要**把「登录角色」之类服务端能自己推断的字段塞进请求体。
  踩过的坑：后端 `Literal["visitor","student","employee","manager"]` 漏了 `admin`，
  前端又原样传了 `role`，结果管理员一发言就 422。前端不传、服务端以 Token 为准，
  这类 bug 整类消失。
- CORS 别写成 `allow_origins=["*"] if debug else []`——`debug` 默认 False 时
  等于分域部署直接失效。用可配的 `CORS_ORIGINS` 白名单，默认把前端端口列进去。
- 前端换后端地址的能力要留出来（`localStorage` 存 `api_base`），并且**后端不可达时
  要能优雅回到登录屏提示**，而不是白屏。这条值得专门写个 E2E 断言。
- ⚠️ **往 `.env` 加 List/Dict 型配置会让 FastAPI 起不来**。pydantic-settings 对复杂字段
  默认要求 env 值是 JSON，且在**数据源层**就先 `json.loads`（早于字段校验器，
  所以 `field_validator(mode="before")` 救不了）。于是

  ```env
  CORS_ORIGINS=http://127.0.0.1:8020,http://localhost:5173   # 直觉写法 → 直接崩
  ```

  会抛 `SettingsError: error parsing value for field "cors_origins"`。
  解法：字段标 `Annotated[List[str], NoDecode]`（pydantic-settings ≥ 2.3），
  再用 `field_validator(mode="before")` 同时接受逗号分隔与 JSON 数组。
  **最阴的是这坑只在有 `.env` 时才炸**——测试跑在代码内默认值上会全绿，
  所以务必写一条「拿真实 `.env.example` 构造 Settings」的回归测试。

## 七、多个「页面状态」同屏只显示一个时

「访客落地页 / 登录屏 / 控制台」这种三选一，不用路由库，三个顶层 `div` + `[hidden]` 就够：

- **默认要显示的那个 div 不要写 `hidden`**，其余两个写上。这样 JS 一挂，用户至少还能看到内容页
  而不是白屏，首屏也不会闪一下再切换。
- 每个 `showX()` 只做一件事：显示自己、把另外两个 `hidden`。别在别处顺手改 `hidden`，
  否则会出现两个页面同时可见 / 都不可见。
- 横跨多个页面共用的动作（如切换主题）抽成独立函数 `toggleTheme()`，别在两个按钮回调里各写一份。
- 出口按钮统一用 `data-*` + 事件代理（`document.querySelectorAll('[data-landing]')`），
  同一个动作在导航栏 / Hero / 底部条各放一份也不用重复绑事件。
- 退出与「会话失效」要分开：**主动退出回落地页，401 失效回登录屏并带原因提示**。
  两者都回登录屏的话，访客（无账号）被 401 后会被卡在一个他填不了的表单上。

## 八、跑「多轮对话」E2E 时，等待条件别写成「最后一条气泡够长」

流式聊天页的 E2E 最容易写成一条**偶发失败**（本地跑三次过两次，CI 上随机挂）：

```js
// ❌ 看着没问题，其实是竞态
const waitAnswer = () => page.waitForFunction(() => {
  const els = document.querySelectorAll('.msg.assistant .msg-bubble');
  const t = els.length ? els[els.length - 1].textContent.trim() : '';
  return t.length > 8 && !t.startsWith('【');
});
await ask('第一句');
await ask('第二句');   // ← 随机失败的那一步
```

**为什么错**：流式是逐块追加的，「字数 > 8」在第一句**还没生成完**时就成立了。
`waitAnswer` 提前返回 → 紧接着的 `ask()` 撞上前端的 `state.streaming` 守卫 →
被静默吞掉（只弹个 toast，**不产生任何气泡**）→ 后面的断言读到的是上一轮的页面状态。

**正确判据**是「这轮真的结束了」，两个条件都要：

```js
const waitIdle = () => page.waitForFunction(() => {
  const btn = document.querySelector('#chat-send');
  return !!btn && !btn.disabled;          // 生成期间发送按钮是 disabled 的
}, { timeout: 60000 }).catch(() => {});

const waitAnswer = () => page.waitForFunction(() => {
  const btn = document.querySelector('#chat-send');
  if (!btn || btn.disabled) return false;                              // 还在生成
  if (document.querySelectorAll('.msg').length <= (window.__e2eMsgCount || 0)) {
    return false;                                                     // 新气泡还没出现
  }
  const els = document.querySelectorAll('.msg.assistant .msg-bubble');
  const t = els.length ? els[els.length - 1].textContent.trim() : '';
  return t.length > 8 && !t.startsWith('【');
}, { timeout: 60000 }).catch(() => {});

const ask = async (text) => {
  await waitIdle();                                  // ① 上一条必须已收尾
  await page.evaluate(() => {                        // ② 记下本轮开始前的气泡数
    window.__e2eMsgCount = document.querySelectorAll('.msg').length;
  });
  await page.click('#chat-text');
  await page.type('#chat-text', text);
  await page.click('#chat-send');
  await waitAnswer();                                // ③ 等「新气泡 + 已收尾」
  await sleep(300);
};
```

要点：

- **把「开始前的气泡数」通过 `page.evaluate` 写进页面全局**，而不是当参数传给
  `waitForFunction` —— 后者的签名在不同 puppeteer 版本里挪过位（`(fn, options, ...args)`
  vs `(fn, args, options)`），写错了会静默不生效。
- 轮询函数里**不要 `throw`**，返回 `false` 即可；超时用 `.catch(() => {})` 兜住，
  再由断言给出可读的失败信息。
- 断言「页面状态面板 / 路由结果」这类**由上一轮异步事件更新的 DOM**时，
  一定要先 `waitIdle()`。只等气泡的话，读到的可能是上一轮的值 —— 而且看起来「有内容」，
  所以失败信息会很有误导性（本次就是面板停在上一句，被误判成路由回归）。
- 复盘时区分「真回归」和「竞态」的最快办法：**直接打后端接口**复现同一句话。
  接口返回正确 → 问题在等待条件，不在业务逻辑。

## 九、自带脚本

| 文件 | 用途 |
|---|---|
| `scripts/run_with_servers.py` | 起后端+前端 → 跑 node 脚本 → 收服（单次调用编排器，见第二节）。`python run_with_servers.py scripts/frontend_e2e.mjs` |
| `scripts/check_frontend_imports.py` | L2 导入一致性，退出码 0/1 |
| `scripts/frontend_e2e.mjs` | L3 浏览器 E2E + 截图 + JSON 报告，退出码 0/1 |
| `scripts/serve_frontend.py` | 前端静态服务器 |
| `scripts/check_syntax.ps1` | L1 语法检查（复制成 .mjs 后 `node --check`） |

用之前把 `frontend_e2e.mjs` 里的 `FRONT` / `BACK` / 选择器 / 断言换成目标项目的。
