---
name: windows-python-one-click-launcher
description: 给 Windows 上的 Python Web 项目（FastAPI/uvicorn + 可选前端静态服务）做「双击即跑」的一键启动方案：scripts/launcher.py + start.bat / stop.bat，覆盖找解释器、查依赖、生成 .env、建库灌种子、挑空闲端口、起前后端、探活、开浏览器、停服、状态、自检。当用户要求「让项目跑起来」「生成一键启动脚本」「做个启动 bat」「双击就能用」时使用；也用于排查「双击 start.bat 后弹窗报『不是内部或外部命令』」。内含 cmd.exe 批处理陷阱、Popen 传 list 导致引号被 `\"` 转义的引号事故、沙箱进程回收（含「要让服务在沙箱里常驻」的正确解法）、端口默认值漂移，以及不可复现路径的等价替代验证法的实测结论。
agent_created: true
---

# Windows Python 项目的一键启动脚本

目标：非开发机用户**双击一个 `start.bat`** 就能把项目跑起来，失败时能看到原因。

结构固定为三件套：

```
项目根/
├── start.bat          # 双击入口（纯 ASCII），负责找到 python 并转发参数
├── stop.bat           # 双击停止
├── scripts/launcher.py  # 真正的逻辑，所有中文输出都从这里来
└── .run/              # 运行态：pids.json + logs/（写进 .gitignore）
```

**为什么逻辑放 Python 而不是 bat**：`.bat` 由 cmd.exe 按 ANSI 读取，
写中文必然乱码，而且它没有 try/except、没有 urllib、没有 json。**bat 只做两件事**：
找到解释器、把参数转过去。

---

## 一、launcher.py 的十步骨架

按顺序做，任何一步失败就**明确报错 + 给出下一步建议**，不要静默退出。

| 步 | 做什么 | 关键点 |
|---|---|---|
| 1 | 定位解释器 | 顺序：项目 `.venv` → 隔离环境 → `shutil.which`。别只信 PATH |
| 2 | 查依赖 | 子进程跑 `importlib.util.find_spec`，**返回缺失清单再决定装不装**，不要无脑 `pip install -r` |
| 3 | 生成 `.env` | 没有就从 `.env.example` 复制，别让用户手抄 |
| 4 | 建库灌种子 | 库文件不存在时自动跑 seed；`--reseed` 才重置 |
| 5 | 挑端口 | 见第二节 |
| 6 | 起进程 | 见第三节 |
| 7 | 探活 | 轮询健康接口，`print(".", flush=True)` 做进度点，超时给 90s（首次要建表） |
| 8 | 落 PID | `.run/pids.json`，`--stop` 靠它精确收服 |
| 9 | 打印横幅 | 前端地址 / 接口文档 / 健康检查 / 演示账号，一次说清 |
| 10 | 开浏览器 | `webbrowser.open(front_url)`，仅在探活成功后 |

子命令用 `argparse` 平铺：`--stop` / `--status` / `--selfcheck` / `--headless` /
`--reseed` / `--smoke` / `--no-browser` / `--backend-port` / `--frontend-port` / `--python`。

---

## 二、端口：探针要能认出「自己人」

只判「端口是否空闲」会犯两种错：把别人的服务当自己人，或自己已经在跑却起第二套。

```python
def pick_port(preferred, kind):
    probe = backend_alive if kind == "backend" else frontend_alive
    if probe(preferred):                 # 已经在跑 → 复用，不重启
        return preferred, True
    if port_is_free(preferred):
        return preferred, False
    for off in range(1, 21):             # 被占用 → 往后找
        cand = preferred + off
        if probe(cand):
            return cand, True
        if port_is_free(cand):
            return cand, False
    raise RuntimeError(...)
```

`backend_alive` 不能只看「有响应」，要校验**业务特征**——本项目返回的是
`{"code":0,"data":{...,"app":...}}`，就断言 `code == 0 and "app" in data`。
前端则断言页面里有某个固定中文串。

`port_is_free` 用 `socket.bind` 而不是 `connect`（connect 对 TIME_WAIT 会误判）。
查占用 PID 用 `netstat -ano -p TCP` + 解析 `LISTENING` 行，**不依赖 psutil**。

**换端口必须通知前端**：把选中的后端地址通过静态服务器的 `--api-base` 注入
（见 `zero-build-frontend-verify` 技能第三节），否则换端口就白屏。

---

## 三、两种派生方式，各有用途

### console 模式（默认，适合真人）

前后端各弹一个窗口，日志实时可见，**关掉窗口即停服** —— 这是给用户最好的心智模型。

> 🔴 **这里有一个必须避开的坑：`Popen` 的参数绝对不能传 list。**
> 传 list 时 Python 会先用 `subprocess.list2cmdline` 拼命令行，它会把 `cmdline`
> 整体包引号、并把**内层引号转义成 `\"`**；而 **cmd.exe 不认反斜杠转义**，
> 于是它去找一个字面名叫 `\"C:\...\python.exe\"` 的文件，报
> 「不是内部或外部命令，也不是可运行的程序或批处理文件」。
> 现象极具迷惑性：窗口**标题正常设上了**（`title` 那半句成功了），
> 紧接着一句报错、服务起不来。**报错信息里出现 `\"` 就是铁证。**

正确做法：把整行**拼成字符串**再交给 `Popen`（字符串 = 原样送 `CreateProcess`，
不再二次转义），并给含空格/cmd 元字符的参数自己加引号：

```python
def _cmd_quote_arg(arg: str) -> str:
    """按 cmd.exe（不是 C runtime）的规则加引号。"""
    if arg == "":
        return '""'
    if any(ch in arg for ch in ' \t"&()[]{}^=;!\'+,`~'):
        return f'"{arg}"'
    return arg


def console_cmdline(title: str, py: str, args: list[str]) -> str:
    inner = " ".join([f'"{py}"', *(_cmd_quote_arg(a) for a in args)])
    return f'cmd /k "title {title} & {inner}"'


Popen(console_cmdline(title, py, args),      # ← 字符串，不是 list
      creationflags=CREATE_NEW_CONSOLE, cwd=PROJECT)
```

原理：`cmd /k "title X & "C:\path\py.exe" args"` 里引号数 > 2，
cmd 的规则是**掐掉最外层首尾两个引号**，剩下
`title X & "C:\path\py.exe" args` 正好可正常解析。

**用 `&` 不用 `&&`**：`title` 只是改窗口标题的装饰动作。若用 `&&`，
万一 `title` 失败（例如在没有 console 的环境里），后面的服务**根本不会启动** ——
把「装饰失败」放大成「服务起不来」。`&` 是无条件继续。

**把拼接逻辑单独抽成函数**（不要内联在 `Popen` 里），否则没法写回归测试 ——
见第六节第 7 条。

### headless 模式（适合 CI / 无桌面 / 沙箱）

```python
handle = open(LOG_DIR / f"{name}.log", "wb")
Popen(cmd, stdout=handle, stderr=STDOUT, stdin=DEVNULL,
      creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW)
handle.close()          # 子进程已持有自己的句柄
```

失败时把日志尾部打出来，否则用户拿不到任何线索。

### selfcheck 模式（起服 → 体检 → 收服，单进程）

**不要**用「先起服、再另起一次命令做检查」的两步法，理由见第五节。
正确写法是在一个 python 进程里 `Popen` 子进程（不加任何 detach 标志）→
探活 → 打接口 → `terminate()` → `wait()`，最后输出体检表。

体检项至少覆盖：前后端探活、`/health`、`/ready`、**登录换 token**、
**无 token 访问受保护接口应 401**、`/docs`、前端 `index.html`/主模块/CSS 可加载。
这套指标能一次性区分「端口通了」和「服务真能干活」。

收服用 `taskkill /F /T /PID`（`/T` 连子进程树）；自检模式里直接
`proc.terminate() + wait(timeout=10)` 就够，因为没开 detach。

### `--stop` 必须按端口兜底，不能只信 PID 记录

实测踩坑：`.run/pids.json` 里的 PID **会失效**（控制台窗口被用户关过、
进程被系统回收过、或上一次是用别的模式起的）。此时老写法会打印
「backend（pid=…）可能已经退出」，但服务其实**还在跑、端口一直被占**，
用户再点 `start.bat` 只能换端口，越堆越多。

正确做法是两段式清理，且**兜底那一段要先确认是自己人才动手**：

```python
def stop_by_port(port: int, kind: str) -> list[int]:
    """只杀探针认得的自家服务，避免误伤占用同端口的别人。"""
    probe = backend_alive if kind == "backend" else frontend_alive
    if not probe(port):          # 探针不认 → 不是我们的，别碰
        return []
    return [pid for pid in pid_on_port(port) if kill_tree(pid)]
```

`cmd_stop()` 里：先按 PID 清理（失败只 warn 不放弃）→ **再用
`stop_by_port()` 扫一遍端口**（无论 PID 记录是否存在都扫）→ 删 `pids.json`。

为什么必须带探针：端口是共享资源，我们的进程死后**别人可能立刻占用同一端口**，
无脑 `taskkill` 会误杀无辜进程。探针校验业务特征（后端认 `code==0 && data.app`，
前端认页面含项目名）既是「端口避让」的依据，也是「清理」的凭据。

验证方法：删掉 `pids.json` → 起服 → `--stop`，应打印
「按端口清理了残留的 backend（端口 8010，pid=…）」，且 `--status` 转为 down、
端口 `listening=False`。

---

## 四、cmd.exe 批处理陷阱（全是实测）

| 陷阱 | 现象 | 解法 |
|---|---|---|
| **`if (...)` 块里的 `echo` 带 `)`** | 报 `---- was unexpected at this time`，退出码 255 | `)` 会被当成块结束符提前闭合。echo 文案里**不要出现括号**，或写 `^(` `^)` |
| 中文写进 `.bat` | 乱码 | `.bat` 保持纯 ASCII，中文一律由 python 打印 |
| 显示中文乱码 | 方块 / 问号 | `chcp 65001 >nul` + `set PYTHONIOENCODING=utf-8` + `set PYTHONUTF8=1` |
| 退出码丢失 | `%errorlevel%` 取到的是 `echo` 的 | 紧跟在命令后立刻 `set "RC=!errorlevel!"`（需 `setlocal enabledelayedexpansion`） |
| 双击后窗口一闪而过 | 看不到任何信息 | 只有「无参数启动」（=双击）时才 `pause`，命令行带参数时不 pause，否则脚本无法自动化 |
| `start.bat` 与内建 `start` 冲突 | —— | 带扩展名调用即可（`.\start.bat`）；测试时也从 python `subprocess` 里用 `cmd /c .\start.bat` |
| 变量在块内不更新 | `%VAR%` 是解析时的旧值 | 块内统一用 `!VAR!` |
| **`Popen(["cmd","/k",cmdline])` 传 list** | 弹出的窗口报「`\"C:\...\python.exe\"` 不是内部或外部命令」；窗口标题却正常 | `list2cmdline` 把内层引号转义成 `\"`，cmd 不认反斜杠转义。**整行拼成字符串再传**，详见第三节 |
| 路径含空格 / 中文 | 时好时坏 | 解释器路径**永远**加引号；参数含空格或 `&()[]{}^=;!'+,` 加引号（第三节 `_cmd_quote_arg`） |

`pause` 的判据要显式存下来：

```bat
set "PAUSE_ON_ERR="
if "%~1"=="" set "PAUSE_ON_ERR=1"     rem 无参数 = 双击
```

---

## 五、⚠️ 沙箱/受控环境会回收整棵进程树（实测结论）

**在做自动化验证时必须知道这条，否则会得出错误结论「启动器坏了」。**

实测（三种派生方式，父进程退出后由另一次工具调用去检查标记文件）：

| 派生方式 | 结果 |
|---|---|
| `DETACHED_PROCESS │ CREATE_NO_WINDOW` | 子进程未写出标记文件（被杀） |
| `CREATE_NEW_CONSOLE` + `cmd /k` | 同上 |
| `CREATE_NEW_CONSOLE │ CREATE_BREAKAWAY_FROM_JOB` | 同上 |

结论：**在沙箱里，任何被派生的进程都会在本次工具调用结束时被回收。**
这不代表 console 模式有问题——真人双击 `start.bat` 时不受此限制——
但意味着：

- 不能用「起服（调用 A）→ 验证（调用 B）」的方式验证；
- 一定要用 `--selfcheck` 这类**单次调用内完成**的模式来自证；
- 看到「PID 不见了、端口没监听、窗口里也没有报错」时，先怀疑进程树回收，
  别急着改代码。

判断方法：把持久化方式换成「起一个写标记文件的子进程 + sleep 90s」，
父进程退出后另起一次调用检查标记文件是否存在。

#### 例外与解法：确实要让服务在沙箱里**常驻**时怎么办

上面是「验收」场景的结论。但有时服务确实需要一直活着（后续还要连跑多条
冒烟/端到端用例，或要给用户一个能点的地址）。这时**只靠 `DETACHED_PROCESS`
一定是白搭** —— 必须让「最外层那条命令不退出」，把整个 job 撑住：

```bash
# 放在后台任务机制里跑。launcher 起完服务就自己退出，由末尾的 sleep 撑住 job。
python scripts/launcher.py --headless --no-browser; exec sleep 31536000
```

实测有效：`spawn_headless` 派生的 backend/frontend 全程存活，
`--status` 两个 `[OK]`，冒烟 149/149 通过。

反面写法（全部失败）：`nohup ... &`、`start /b`、以及任何
「命令跑完就返回」的组合 —— job 一关，子进程无论怎么 detach 都被收走。

代价：后台任务里会留一个 `sleep` 进程。停服时除了 `python scripts/launcher.py --stop`，
还要把那个后台任务本身停掉，否则 sleep 一直挂着。

**⚠️ 实测补充：这只在「同一轮次内」有效。** 用户发下一条消息（新的 agent turn）
之后再去检查，服务已经没了 —— **后台任务不跨轮次保活**。所以：

- 要「起服 → 验证」必须在**同一次用户请求内**跑完；
- 想给用户一个长期可访问的地址，只能让用户**自己双击 `start.bat`**，
  真人操作不受沙箱进程回收约束；
- 看到「上一轮明明 `--status` 全 OK，这一轮连接被拒」，先怀疑跨轮次回收，
  别去改启动器代码。

### 推论：**console 那条路径 `--selfcheck` 验不到，必须用「等价替代」验**

上面这条回收结论带来一个容易忽视的后果：`--selfcheck` 走的是
`CREATE_NO_WINDOW` + 直接 `Popen([py, *args])`，**压根不经过 `cmd /k` 的拼串逻辑**。
所以 `--selfcheck` 全绿 ≠ 双击时的 console 窗口能起来 ——
真事故就是这样漏过去的：自检 13/13 通过，用户双击却弹窗报错。

**不要因为「沙箱里弹不出窗口」就跳过这条路径的验证。** 用等价替代：

```python
# 把拼好的命令行交给 cmd 执行（/k 换成 /c，免得 cmd 常驻把测试挂住）。
# 除 /k→/c 外与线上完全一致，足以证明引号拼装是对的。
raw = console_cmdline(title, py, args)
subprocess.run(raw.replace("cmd /k ", "cmd /c ", 1),
               capture_output=True, text=True, encoding="utf-8",
               errors="replace", timeout=120)
# 断言 returncode == 0 且 stub 脚本的 stdout 出现
```

再配一条**反证用例**：用老写法（`Popen` 传 list → `list2cmdline`）跑同一个 stub，
断言它 **rc != 0 且没有输出** —— 钉死「老写法就是坏的」，谁改回去谁红。

判断准则：**凡是无法在沙箱内端到端复现的路径（新控制台窗口、GUI、托盘、
系统对话框），一律找「同一段逻辑的可执行等价物」去验，并留一条反证。**

### 端口默认值不一致，手工跑脚本必须显式传 `--base`

实测踩到：`launcher.py` 默认端口是 **8010 / 8020**
（`DEFAULT_BACKEND_PORT` / `DEFAULT_FRONTEND_PORT`），
但 `smoke_test.py` 的 `--base` 默认值是 **`http://127.0.0.1:8000`**。

两边都不通：手工在 8000 起服务 → `launcher --status` 看不见它（报 FAIL）；
用 launcher 起在 8010 → 手工跑 smoke 不加参数会全部打空。

规矩：**服务一律交给 launcher，跑检查脚本一律显式传端口**：

```bash
python scripts/launcher.py --headless --no-browser
python scripts/smoke_test.py --base http://127.0.0.1:8010
```

`launcher.py --smoke` 内部会自动带上正确的 `--base`，所以那条路没这个问题 ——
只有手工直接调 `smoke_test.py` 时才要自己记得。

这是真实的「默认值漂移」瑕疵。更彻底的修法是让 `smoke_test.py` 的默认值
跟随 `launcher.py` 的常量，或从 `.run/pids.json` 读 `backend_port`。

---

## 六、验收清单

交付前必须跑齐：

1. `python -m py_compile scripts/launcher.py` 通过；
2. `launcher.py --selfcheck` 全部 **[OK]**，退出码 0；
3. `launcher.py --help`、`--status` 退出码正确（down 时应为 1），
   且**删掉 `.run/pids.json` 后 `--stop` 仍能把服务清干净**
   （只信 PID 的实现在这一步会假报「已经退出」但端口仍被占）；
4. **从 `cmd /c .\start.bat --help` 跑一遍**，确认 bat 包装层没有解析错误、
   退出码能透传、中文不乱码；
5. `start.bat --selfcheck`（或 `--smoke`）在真实起服下全绿；
6. README 里有一键启动段落，且**新克隆仓库也能照做**（`.env.example` 必须能被解析，
   否则「生成 .env」这一步直接让项目起不来）。
7. **单测覆盖 `console_cmdline()` 拼装**，且至少包含：
   ① 拼出的串里**不含 `\"`**（回归标记）；② 脏参数（带空格、带 `&`）被引起来；
   ③ **真把命令行交给 cmd 跑一遍**（`/k`→`/c`）断言 rc=0；
   ④ **反证**：老写法（传 list）确实 rc≠0。
   第 3、5 条验不到 console 路径，**只有第 7 条能挡住双击弹窗报错**。

第 6 条容易漏：`.env.example` 里有 List/Dict 型配置时，
务必加一条「拿真实 `.env.example` 构造 Settings」的回归测试。
