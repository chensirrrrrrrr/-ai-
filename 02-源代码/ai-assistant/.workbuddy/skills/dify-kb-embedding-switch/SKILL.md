---
name: dify-kb-embedding-switch
description: 不删知识库、不动检索节点的前提下，把 Dify（1.x）数据集的**嵌入模型原地换掉**（本地 Ollama/bge-m3 → 云端智谱 embedding-3，或任意换供应商）。覆盖：装模型供应商插件、写 provider 凭据、PATCH 数据集的三字段硬约束（漏 `indexing_technique` 直接 500）、「换模型=换向量空间、必须重建索引」、以及**用 hit-testing 命中证明真的换了**（而不是只看配置回显）。当用户说「知识库换嵌入模型」「embedding 切云端」「Dify 不再依赖本地模型」时使用。
agent_created: true
---

# Dify 知识库嵌入模型「原地切换」

目标：把某个 dataset 的嵌入模型换成另一个（换供应商 / 本地换云端），
**不重建知识库、不重新登记文档、不改画布上任何检索节点**。

## 一、三条硬事实（先记住，能省掉一半试错）

1. **换嵌入模型 = 换向量空间**。不同模型的向量维度与语义空间都不同
   （实测：本地 `bge-m3` 维度 **1024**，智谱 `embedding-3` 维度 **2048**）。
   旧向量在新模型下没有意义，**索引必须整体重建**。
2. **`dataset_id` 不变** ⇒ 画布上的 knowledge-retrieval 节点、
   后端的「知识文档登记」功能**都不用动**。这是「原地切换」成立的根本原因。
3. **Dify 会自动重建**。PATCH 数据集时若检测到嵌入模型变化，控制面会触发
   `deal_dataset_vector_index_task`（`app/api/services/dataset_service.py` →
   `_handle_indexing_technique_change()`），无需手动清索引。

⚠️ Weaviate 集合名只由 `dataset_id` 决定（`Vector_index_<dataset_id>_Node`），
**维度变化不会体现在集合名上** ⇒ 不能靠看集合名判断是否换成功，必须用命中验证。

## 二、五个坑（每个都真实踩过）

| # | 坑 | 症状 | 解法 |
|---|---|---|---|
| 1 | **PATCH 体漏 `indexing_technique`** | 接口 **500**（不是 422 参数校验） | 更新知识库配置的 handler **直接读 `data["indexing_technique"]`**，缺了就 KeyError。请求体必须同时带 `indexing_technique` + `embedding_model` + `embedding_model_provider` 三个字段 |
| 2 | **供应商插件没装** | 目标模型在下拉里根本不存在；PATCH 报模型不可用 | 先从 marketplace 装插件（见步骤 1），并轮询安装任务到 `success` |
| 3 | **凭据没写** | PATCH 或检索报模型凭据无效 | provider 装好 ≠ 可用，还要 `POST .../credentials` 写入 `api_key` |
| 4 | **只看配置回显就以为切好了** | `GET /datasets/<id>` 显示新模型，但检索命中不到 / 答非所问 | 配置变更与重建是两件事。**必须等重建完成 + 做一次真实 hit-testing**（见步骤 5） |
| 5 | **直接查 Weaviate 被 403** | `forbidden` | Weaviate 需要 `Authorization: Bearer <WEAVIATE_API_KEY>`，key 从 api 容器环境变量取 |

## 三、操作步骤（控制面 API，需 cookie + `X-CSRF-Token`）

> 控制面基址是 `http://<host>`（不带 `/v1`），与应用 API 的 `http://<host>/v1` 不是一回事。
> 登录：`POST /console/api/login {email, password, language:"zh-Hans", remember_me:true}`，
> 之后所有请求带 `X-CSRF-Token: <cookie 里的 csrf_token>`。

**0. 先看现状**（拿到两个真值，回滚时要用）

```
GET /console/api/datasets/<dataset_id>
→ 记下 embedding_model / embedding_model_provider / indexing_technique
```

**1. 装供应商插件（若未装）**

```
POST /console/api/workspaces/current/plugin/install/marketplace
     {"plugin_unique_identifiers": ["langgenius/zhipuai:0.0.35@<sha256>"]}
→ 返回 task_id，轮询 GET /console/api/workspaces/current/plugin/tasks/<task_id>
   直到 detail.status ∈ {success, failed}
```
`plugin_unique_identifier` 形如 `<org>/<plugin>:<version>@<sha256>`，可从 marketplace 接口取。
（智谱：`langgenius/zhipuai`；官方文档说模型供应商名在 provider 字段里要写全 `langgenius/zhipuai/zhipuai`。）

**2. 写 provider 凭据**

```
GET  /console/api/workspaces/current/model-providers/langgenius/zhipuai/zhipuai
     → 看 custom_configuration.schema.credentials_schema，确认字段名（通常是 api_key）
POST /console/api/workspaces/current/model-providers/langgenius/zhipuai/zhipuai/credentials
     {"credentials": {"api_key": "<KEY>"}}
```
🔴 **Key 只写进 Dify 自己的库，绝不落进项目文件 / 脚本 / 日志**（脚本从环境变量读）。

**3. 确认目标模型真的可用**

```
GET /console/api/workspaces/current/models/model-types/text-embedding
→ 找到 {"provider":"langgenius/zhipuai/zhipuai","models":[{"model":"embedding-3"}, ...]}
```
拿不到目标模型就别往下走（多半是步骤 1/2 没成）。

**4. PATCH 数据集（一步到位）**

```
PATCH /console/api/datasets/<dataset_id>
      {"indexing_technique": "high_quality",
       "embedding_model": "embedding-3",
       "embedding_model_provider": "langgenius/zhipuai/zhipuai"}
```
成功后会**自动触发全量重建**。多个知识库就逐个 PATCH。

**5. 等重建 + 命中验证（关键一步，不能省）**

```
# a) 文档索引状态：应全部 completed
GET /console/api/datasets/<id>/documents?page=1&limit=100
   → 按 indexing_status 计数，出现 error / indexing 就继续等

# b) 命中测试：能召回相关内容才说明新模型已重新嵌入
POST /console/api/datasets/<id>/hit-testing  {"query": "<业务词>"}
   → records[].score 通常 0.4~0.6；内容与 query 相关即通过
```
**判据**：`completed` + 至少一条相关命中。只看 a) 不够（索引可能空转），
只看配置回显更不够。

## 四、回滚

反向 PATCH 回原 provider/model 即可（同样自动重建）。
所以**步骤 0 一定要把原值记下来**。历史日志里应保留原配置（本项目把 bge-m3 配置留在 09-16 日志里）。

## 五、什么时候不该换

- 只是想让「检索更准」：先调分块与检索参数（top_k / score_threshold / rerank），换嵌入模型是最后手段。
- 想彻底不依赖外网：那就别换云端——换成云端嵌入 = 建索引与检索都需要出网。
  反过来，**只要对话模型与嵌入模型都在云端，就完全不需要本地常驻模型（Ollama 可停）**。
- 有大量向量、又想省钱：优先看平台的「经济索引」，而不是换模型。

## 六、自带脚本

| 文件 | 用途 |
|---|---|
| `scripts/kb_embed_switch.py` | 看现状 → PATCH 切换 → 轮询重建 → hit-testing 验证，一条龙。凭据从环境变量读，默认 `--dry-run` |

```bash
export DIFY_CONSOLE_EMAIL=... DIFY_CONSOLE_PASSWORD=... DIFY_CONSOLE_BASE=http://127.0.0.1
python kb_embed_switch.py --dataset <id> --provider langgenius/zhipuai/zhipuai --model embedding-3            # 预演
python kb_embed_switch.py --dataset <id> --provider langgenius/zhipuai/zhipuai --model embedding-3 --apply    # 执行
```

## 七、相关技能

- `mock-first-external-service` —— 接外部 SaaS 的 mock/live 双模与 preflight；
  本技能是其「平台侧模型配置」的具体分支。
- `dify-workflow-draft-node-repair` —— 同一平台上的另一个高频坑（画布节点白条）。
