---
name: idempotent-demo-seed
description: 为有数据库的项目写「幂等演示数据播种脚本」（scripts/seed.py + --reset），让演示账号与示例数据可以反复灌、跑测试前自动准备、断言有稳定预期。包含守卫式插入、自然键关联、计数回报、演示账号表、以及 pytest 夹具复用同一个 seed 的写法。当需要准备演示数据/测试数据、或遇到「跑第二遍就报主键冲突」「测试数据偶发不一致」时使用。
agent_created: true
---

# 幂等演示数据播种

**目标**：一条命令把演示数据准备好，而且**可以跑第二遍、第三遍**都不出错、不重复、不乱。

```
python scripts/seed.py            # 追加缺失数据（幂等）
python scripts/seed.py --reset    # 先 drop_all 再重建（慎用）
```

看起来简单，但这里的错误会以最难查的方式暴露：**跑第二遍主键冲突**、
**测试偶发失败**（因为数据被上一轮污染）、**E2E 断言飘**（因为数据每次都不一样）。

---

## 一、三条硬要求

| 要求 | 判据 | 不满足时的症状 |
|---|---|---|
| **幂等** | 连跑 3 次，`<表>.count()` 完全一致 | 主键冲突 / 数据翻倍 |
| **可重置** | `--reset` 能把库回到干净的种子态 | 改过 schema 后本地库起不来 |
| **可断言** | 结尾打印各表计数；演示账号是一张固定表 | 测试只能靠"大概应有数据" |

---

## 二、守卫式插入：每个实体块先问「有没有」

```python
if not db.query(Employee).filter(Employee.emp_no == "E001").first():
    db.add_all([...])
    db.flush()
```

用**自然键**做守卫（`emp_no` / `student_no` / `username`），而不是「表里有没有数据」：

```python
# ❌ 坏：表非空就整块跳过。后续新增一条员工时，老库永远补不上
if not db.query(Employee).first(): ...

# ✅ 好：按自然键逐条判，新加的种子会被补进来
for no, name, dept in EMPLOYEES:
    if not db.query(Employee).filter(Employee.emp_no == no).first():
        db.add(Employee(emp_no=no, name=name, department=dept))
```

结构上按**实体分块**，块与块之间互不依赖顺序（除了真的需要外键的那几对）。
这样以后加一类种子数据，就是加一个块，不动存量。

---

## 三、⚠️ 别硬编码自增 id

本项目最初的 `seed.py` 里有 `advisor_id=1`、`StudentScore(student_id=1, ...)` 这类写法。
它**能跑**，但埋了两个雷：换库/换插入顺序后 1 不再是那个人；`--reset` 与增量灌的 id 可能不同。

正确做法是**插入 → flush → 用自然键查回来**：

```python
db.add_all(EMPLOYEES)
db.flush()                                            # 拿到自增 id，但不硬编码
admin  = db.query(Employee).filter(Employee.emp_no == "E001").one()
leader = db.query(Employee).filter(Employee.emp_no == "E003").one()
admin.manager_id = leader.id                          # 关联走对象，不写字面量

student_zhang = db.query(Student).filter(Student.student_no == "S2026001").one()
db.add(StudentScore(student_id=student_zhang.id, exam_name="雅思第一次", score=6.5))
```

`flush()`（不是 `commit()`）就够了：把 INSERT 发下去拿到 id，事务还没提交，
后面出错可以整体回滚。

---

## 四、数据必须**确定**，不能随机

`random.choice` / `datetime.now()` 满天飞的种子会让两类东西失效：

- E2E 断言（"列表里第 3 条应该是 XX"）
- 人工演示时的讲解稿（"看这里，王敏名下 2 个学生"）

规则：

- 需要的随机性**固定种子**（`random.Random(20260914)`），或干脆写死。
- 和"现在"有关的字段用**相对偏移**而不是绝对值，且偏移量固定：
  ```python
  now = datetime.now()                       # 只在这里取一次
  start_at=now + timedelta(days=5)           # 活动永远在 5 天后，演示时永远是"即将开始"
  ```
  绝对日期（`date(2026, 10, 1)`）会让数据随时间推移逐渐失去意义。
- 容易过期但必须写死的（考试日期、报名截止）集中成模块级常量，方便整体顺延。

---

## 五、演示账号单独列一张表

```python
ACCOUNTS = [
    # username, password,    role,      ref_id, display_name
    ("admin",   "admin123",   "admin",   None,   "系统管理员"),
    ("manager", "manager123", "manager", 3,      "陈总（管理层）"),
    ...
]
```

- 这张表是**唯一的真相**：CLI 结尾的提示、README、前端登录页的演示账号卡片、
  E2E 里的登录步骤、`tests/conftest.py` 的凭据字典，都应从它派生或与它逐条对齐。
- 密码用弱口令 + 固定值（演示要能背下来），但在 README 里**明确写"上线前必须全部重置"**，
  `SECRET_KEY` 同理。这条不写进文档，早晚会带着弱口令上线。
- `ref_id` 这种"指向业务档案"的字段，改造成上面第三节的自然键写法更稳。

---

## 六、结尾回报计数，别默默成功

```python
counts = {"employee": db.query(Employee).count(),
          "student": db.query(Student).count(),
          "account": db.query(SysAccount).count()}
print("[seed] 完成：", counts)
print("[seed] 演示账号：admin/admin123  manager/manager123  ...")
```

理由：播种最常见的失败是**"没报错但也没灌进去"**（守卫条件写错、事务没提交、
连到了另一个库）。打印计数能在第一时间发现"数量没变"。

调试时最常用的两条命令：

```bash
python scripts/seed.py --reset && python scripts/seed.py   # 连跑两次 + reset，验幂等
```

---

## 七、让 pytest 复用同一个 seed

**不要为测试另写一份造数逻辑**——两份逻辑一定会漂移，然后测试绿而演示崩。

```python
# tests/conftest.py
DB_FILE = ROOT / "data" / "test_ai_assistant.db"
DB_FILE.parent.mkdir(parents=True, exist_ok=True)
# ⚠️ 连 WAL/SHM 一起清掉，否则上一轮的残留数据会带进来
for suffix in ("", "-wal", "-shm"):
    leftover = pathlib.Path(str(DB_FILE) + suffix)
    if leftover.exists():
        leftover.unlink()

os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"   # 必须在 import app 之前

from scripts.seed import seed        # 复用生产同一份种子

@pytest.fixture(scope="session", autouse=True)
def _database():
    drop_all(); seed(); yield; drop_all()
```

三个要点：

1. **测试库必须独立**（`test_*.db`），且**每次会话开始先 drop**。
2. **环境变量要在 `import app` 之前设**——配置对象通常是 `lru_cache` 的进程级单例，
   导入后再改 env 不生效。
3. SQLite 的 `-wal` / `-shm` 要一起删，只删主库文件会带进上一轮的残留。

## 八、自带脚本

| 文件 | 用途 |
|---|---|
| `scripts/seed_template.py` | 幂等播种脚本模板（守卫式插入 + 自然键关联 + 计数回报 + `--reset`） |
