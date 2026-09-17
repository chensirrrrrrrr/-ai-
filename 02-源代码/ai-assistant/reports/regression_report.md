# 全量回归报告

- **执行时间**：2026-09-17 22:57:51
- **执行方式**：`python scripts/regression.py`
- **结论**：❌ 有层未通过

| 层 | 结果 | 明细 | 耗时 |
|---|---|---|---|
| 单元 / 集成（pytest + 覆盖率） | ✅ | 533 passed / 覆盖率 93% | 18.9s |
| 起服体检 + HTTP 冒烟 + 浏览器 E2E | ❌ | 22 项通过 / 1 项失败 | 111.6s |

### 失败明细：起服体检 + HTTP 冒烟 + 浏览器 E2E

- [FAIL] 冒烟测试整体                              == 报告已写入 C:\Users\机械革命\Desktop\留学机构AI助手系统_交付\02-源代码\ai-assistant\reports\smoke_report.md

> 本文件由 `scripts/regression.py` 自动生成，每次全量回归会整体覆盖。
