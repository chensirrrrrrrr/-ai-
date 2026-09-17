"""REQ-M1-03 / 04 / 05：《用户画像研判规则》的导入、版本化与本地判定。

覆盖点（对应查验报告里的 P0-1 缺口）：
1. 规则可导入、可版本化，且能看出「现在用的是哪一版」；
2. 同一产品换版本时旧版自动归档 —— 「规则调整无需发版」；
3. 双产品**分别**输出是否符合（REQ-M1-04）；
4. 关键字段缺失判「信息不足」而不是「不符合」（避免误杀线索）；
5. 结论能追溯到规则条目 + 原文片段（REQ-M1-05）；
6. 非法规则文档在**导入时**就被拒掉，不静默影响全部研判。

⚠️ 测试用的探针产品 key 是 `rule_probe_*`，跑完即归档 ——
   避免污染会话级共享库里的「硕士直申 / 语言培训」两个正式产品，
   进而影响 test_crm 里对研判结论的断言。
"""
from __future__ import annotations

import pytest

from app.services import rules
from tests.conftest import data

PROBE_KEY = "rule_probe_product"


def _probe_doc(version: str, *, threshold: float = 0.6,
               required=("degree",), logic: str = "all") -> dict:
    """一个只用于测试的规则文档（不影响两个正式产品）。"""
    return {
        "version": version,
        "note": "单元测试探针规则",
        "products": [{
            "key": PROBE_KEY,
            "name": "探针产品",
            "logic": logic,
            "threshold": threshold,
            "required_fields": list(required),
            "rules": [
                {"id": "P-1", "field": "degree", "op": "in",
                 "value": ["本科", "硕士"], "weight": 1.0, "desc": "学历达标"},
                {"id": "P-2", "field": "gpa", "op": "gte",
                 "value": 3.0, "weight": 1.0, "desc": "GPA 不低于 3.0"},
            ],
        }],
    }


def _archive_probe(client, admin_headers):
    """把探针产品产生的所有版本归档，保持共享库干净。"""
    listing = data(client.get("/api/v1/screening/rules", headers=admin_headers))
    for row in listing["items"]:
        if row["product_key"] == PROBE_KEY and row["status"] != "ARCHIVED":
            data(client.post(f"/api/v1/screening/rules/{row['id']}/archive",
                             headers=admin_headers))


# --------------------------------------------------------------------------- #
# 清单 / 模板
# --------------------------------------------------------------------------- #
def test_rules_list_shows_active_version(client, advisor_headers):
    body = data(client.get("/api/v1/screening/rules", headers=advisor_headers))
    assert body["total"] >= 2
    active = body["active"]
    # 两个产品都必须有生效版本，且版本号可读
    assert set(active["versions"]) == {"postgrad_direct", "language_training"}
    assert all(v for v in active["versions"].values())
    assert body["by_status"].get("ACTIVE", 0) >= 2
    # 示例规则必须**自曝身份**，不能让人误以为是甲方口径
    assert "示例" in (active.get("source_name") or "")
    assert active.get("is_sample") is True


def test_rules_template_lists_fields_and_ops(client, advisor_headers):
    body = data(client.get("/api/v1/screening/rules/template", headers=advisor_headers))
    field_keys = {f["key"] for f in body["fields"]}
    # 模板里的字段名必须与本地抽取器完全一致，否则照着填出来的规则永远不命中
    assert {"degree", "gpa", "language", "intention_country"} <= field_keys
    ops = {o["op"] for o in body["ops"]}
    assert ops == {"in", "not_in", "eq", "ne", "gte", "lte", "between", "matches", "exists"}
    assert body["document"]["products"]


# --------------------------------------------------------------------------- #
# 导入 / 版本化 / 权限
# --------------------------------------------------------------------------- #
def test_import_rules_versions_and_archives_old(client, admin_headers, manager_headers):
    try:
        first = data(client.post("/api/v1/screening/rules", headers=manager_headers,
                                 json={**_probe_doc("V1-test"), "activate": True}))
        assert first["imported"] == 1
        assert first["active"]["versions"][PROBE_KEY] == "V1-test"

        second = data(client.post("/api/v1/screening/rules", headers=manager_headers,
                                  json={**_probe_doc("V2-test"), "activate": True}))
        assert second["active"]["versions"][PROBE_KEY] == "V2-test"

        listing = data(client.get("/api/v1/screening/rules", headers=admin_headers))
        probe = [r for r in listing["items"] if r["product_key"] == PROBE_KEY]
        by_version = {r["version"]: r["status"] for r in probe}
        assert by_version["V2-test"] == "ACTIVE"
        assert by_version["V1-test"] == "ARCHIVED"      # 旧版自动归档
    finally:
        _archive_probe(client, admin_headers)


def test_activate_older_version_switches_back(client, admin_headers):
    try:
        data(client.post("/api/v1/screening/rules", headers=admin_headers,
                         json={**_probe_doc("V1-test"), "activate": True}))
        data(client.post("/api/v1/screening/rules", headers=admin_headers,
                         json={**_probe_doc("V2-test"), "activate": True}))
        listing = data(client.get("/api/v1/screening/rules", headers=admin_headers))
        v1 = next(r for r in listing["items"]
                  if r["product_key"] == PROBE_KEY and r["version"] == "V1-test")

        body = data(client.post(f"/api/v1/screening/rules/{v1['id']}/activate",
                                headers=admin_headers))
        assert body["before"] == "ARCHIVED" and body["status"] == "ACTIVE"
        assert body["active"]["versions"][PROBE_KEY] == "V1-test"
    finally:
        _archive_probe(client, admin_headers)


def test_advisor_cannot_import_rules(client, advisor_headers):
    resp = client.post("/api/v1/screening/rules", headers=advisor_headers,
                       json=_probe_doc("V9-nope"))
    assert resp.status_code == 403


def test_import_rejects_bad_document(client, admin_headers):
    # op 不支持（Literal 校验在第一层就拦掉，报「参数校验失败」）
    bad_op = _probe_doc("V-bad")
    bad_op["products"][0]["rules"][0]["op"] = "contains"
    resp = client.post("/api/v1/screening/rules", headers=admin_headers, json=bad_op)
    assert resp.status_code >= 400
    assert resp.json()["message"]

    # 字段名不规范（过得了 Literal，被第二层语义校验拦掉）
    bad_field = _probe_doc("V-bad")
    bad_field["products"][0]["rules"][0]["field"] = "学历"      # 中文标签不是规范键
    resp = client.post("/api/v1/screening/rules", headers=admin_headers, json=bad_field)
    assert resp.status_code >= 400
    assert "字段" in resp.json()["message"]

    # 空 products
    resp = client.post("/api/v1/screening/rules", headers=admin_headers,
                       json={"version": "V-empty", "products": []})
    assert resp.status_code >= 400


# --------------------------------------------------------------------------- #
# 判定：单产品 / 双产品分别输出 / 信息不足 / 可追溯
# --------------------------------------------------------------------------- #
def test_evaluate_product_conform_and_non_conform():
    row = rules.ScreeningRule(product_key="k", product_name="探针", version="V1",
                              logic="all", threshold=0.6,
                              required_fields=["degree"], rules=_probe_doc("V1")["products"][0]["rules"])
    conform = rules.evaluate_product(row, {"degree": "本科", "gpa": "3.6"})
    assert conform["conclusion"] == "符合"
    assert conform["match"] == 1.0

    non = rules.evaluate_product(row, {"degree": "大专", "gpa": "2.1"})
    assert non["conclusion"] == "不符合"


def test_evaluate_product_missing_required_is_insufficient():
    row = rules.ScreeningRule(product_key="k", product_name="探针", version="V1",
                              logic="all", threshold=0.6,
                              required_fields=["degree"], rules=_probe_doc("V1")["products"][0]["rules"])
    out = rules.evaluate_product(row, {"gpa": "3.6"})          # 缺 degree
    assert out["conclusion"] == "信息不足"
    assert out["missing_required"] == ["degree"]
    assert "不判「不符合」" in out["reason"]
    # 信息不足时置信度被压低，避免「没把握却显得很确定」
    assert out["confidence"] <= 0.5


def test_evaluate_two_products_outputs_both(client, advisor_headers):
    body = data(client.post("/api/v1/screening/analyze", headers=advisor_headers,
                            json={"source_type": "TEXT",
                                  "text": "本科，GPA 3.5/4.0，雅思 7.0，目标英国"}))
    detail = data(client.get(f"/api/v1/screening/{body['id']}", headers=advisor_headers))
    products = {p["key"]: p for p in (detail["hit_products"] or [])}
    # REQ-M1-04：两个产品**都**要有结论，不能只留命中的那个
    assert {"postgrad_direct", "language_training"} <= set(products)
    for p in products.values():
        assert p["conclusion"] in ("符合", "不符合", "信息不足")
        assert p["rule_version"]


def test_screening_marks_rule_source_local_and_traceable(client, advisor_headers):
    body = data(client.post("/api/v1/screening/analyze", headers=advisor_headers,
                            json={"source_type": "TEXT",
                                  "text": "本科，GPA 3.5/4.0，雅思 7.0，目标英国"}))
    detail = data(client.get(f"/api/v1/screening/{body['id']}", headers=advisor_headers))
    assert detail["rule_source"] == "local"
    assert "postgrad_direct" in (detail["rule_version"] or "")
    # REQ-M1-05：证据要能落到「哪条规则 + 原文片段」
    assert detail["evidence"]
    assert all("rule" in e and "hit" in e for e in detail["evidence"])


def test_unrecognizable_text_is_insufficient_not_rejected(client, advisor_headers):
    body = data(client.post("/api/v1/screening/analyze", headers=advisor_headers,
                            json={"source_type": "TEXT", "text": "想出国读书，还没想好去哪"}))
    detail = data(client.get(f"/api/v1/screening/{body['id']}", headers=advisor_headers))
    assert detail["conclusion"] == "信息不足"
    assert detail["missing_fields"]                      # 缺什么要说清楚


# --------------------------------------------------------------------------- #
# 纯函数：校验与默认文档
# --------------------------------------------------------------------------- #
def test_default_document_passes_its_own_parser():
    """示例规则必须能被自己的解析器接受 —— 否则 seed 就会炸。"""
    parsed = rules.parse_rule_document(rules.default_rule_document())
    assert len(parsed) == 2
    for item in parsed:
        assert item["required_fields"]
        assert item["rules"]


@pytest.mark.parametrize("doc,keyword", [
    ({"products": "not-a-list"}, "products"),
    ({"products": [{"name": "缺 key", "rules": [{"field": "degree", "op": "exists"}]}]}, "key"),
    ({"products": [{"key": "a", "name": "t", "threshold": 1.5,
                    "rules": [{"field": "degree", "op": "exists"}]}]}, "threshold"),
    ({"products": [{"key": "a", "name": "t", "required_fields": ["不存在的字段"],
                    "rules": [{"field": "degree", "op": "exists"}]}]}, "不是规范字段"),
])
def test_parse_rejects_invalid_documents(doc, keyword):
    with pytest.raises(rules.RuleDocError) as exc:
        rules.parse_rule_document(doc)
    assert keyword in str(exc.value)
