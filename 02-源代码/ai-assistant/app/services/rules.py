"""《用户画像研判规则》的装载、版本化与本地判定（REQ-M1-03 / REQ-M1-04 / REQ-M1-05）。

为什么要有这一层
----------------
SRS 4.1.3 要求「支持导入并版本化管理《用户画像研判规则》，规则调整无需发版」，
4.1.5 又要求「研判必须以甲方提供的规则文件为唯一依据」「结论必须保留可追溯的依据
（引用规则条目 + 原文片段）」。两条加起来意味着：规则**不能硬编码在 python 里**，
而且每次研判都要能回答「这次用的是哪一版规则、命中了哪几条」。

⚠️ 关于随包附带的那份规则
------------------------
甲方那份《用户画像研判规则》**还没拿到**（SRS「待确认事项」第 1、2 条：
规则文件本身、以及「两个产品」的定义与阈值）。所以：

- 本次交付的是**规则引擎 + 导入 / 版本化 / 生效切换的完整能力**（这部分是真的）；
- 随包附带一份 `seed_default_rules()` 的**示例规则**，`source_name` 明确标成
  `示例规则（待甲方《用户画像研判规则》替换）`，研判响应里也会带 `rule_note`
  一起返回 —— 不允许把示例口径当成甲方口径用。
- 甲方文件到位后：转成下面这个 JSON 结构 `POST /screening/rules` 导入即可，
  代码一行不用改（若不是结构化文件，在 `parse_rule_document()` 加一个解析分支）。

规则文档结构
------------
```json
{
  "version": "V1.0",
  "note": "说明文字",
  "products": [
    {
      "key": "postgrad_direct",
      "name": "硕士直申",
      "logic": "all",
      "threshold": 0.75,
      "required_fields": ["degree", "language"],
      "rules": [
        {"id": "R1", "field": "degree", "op": "in",
         "value": ["本科", "硕士"], "weight": 1.0, "desc": "学历需本科及以上"}
      ]
    }
  ]
}
```

支持的 `op`
-----------
`in` / `not_in`（值域命中，字符串含 `|` 分隔时按「任一命中」）
`eq` / `ne` / `gte` / `lte` / `between`（数值；`3.5/4.0` 这种分式取分子）
`matches`（正则）/ `exists`（非空）

判定口径（严格照 SRS 4.1.5 的四条业务规则）
------------------------------------------
1. `required_fields` 里任一字段缺失 → 该产品结论 **「信息不足」**，
   **不允许**直接判「不符合」（业务规则第 2 条），并且必须交人工复核；
2. 一条客户信息对**多个产品分别判定**，允许「同时符合 / 部分符合 / 均不符合」；
3. 每条规则的命中与否连同**原文片段**一起进 `evidence`（可追溯）；
4. 只用规则文件里的标准，不自行放宽。

置信度
------
`confidence = 0.6 * 满足率 + 0.4 * 字段覆盖度`

两个都要：只有「满足率高」但「大部分规则涉及的字段根本没抽到」时，
高置信度是假的（那说明它只是侥幸没被规则否定）。这个公式写在代码里，
界面与报告都能解释这个数是怎么来的，而不是给一个魔术数字。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from ..models import ScreeningRule
from . import material

# 允许的操作符
OPS: Tuple[str, ...] = ("in", "not_in", "eq", "ne", "gte", "lte", "between",
                        "matches", "exists")

# 结论取值（与 schemas / 前端字典一致）
CONFORM = "符合"
NON_CONFORM = "不符合"
INSUFFICIENT = "信息不足"

SAMPLE_SOURCE = "示例规则（待甲方《用户画像研判规则》替换）"


class RuleDocError(ValueError):
    """规则文档结构不合法。报错信息要能直接告诉运维改哪里。"""


# --------------------------------------------------------------------------- #
# 文档解析与校验
# --------------------------------------------------------------------------- #
def _require(cond: bool, message: str) -> None:
    if not cond:
        raise RuleDocError(message)


def parse_rule_document(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """校验并归一化规则文档，返回产品规则列表。

    校验刻意严格：规则导错了会静默影响**所有**研判结论，
    这种错误必须在导入时就炸出来，而不是等业务发现「最近判定怎么都不对」。
    """
    _require(isinstance(doc, dict), "规则文档必须是 JSON 对象")
    products = doc.get("products")
    _require(isinstance(products, list) and products, "规则文档缺少非空的 products 数组")

    version = str(doc.get("version") or "V1.0").strip() or "V1.0"
    note = str(doc.get("note") or "").strip()
    parsed: List[Dict[str, Any]] = []
    seen_keys: set = set()

    for index, item in enumerate(products):
        where = f"products[{index}]"
        _require(isinstance(item, dict), f"{where} 必须是对象")
        key = str(item.get("key") or "").strip()
        name = str(item.get("name") or "").strip()
        _require(bool(key), f"{where} 缺少 key（产品标识）")
        _require(bool(name), f"{where} 缺少 name（产品名）")
        _require(key not in seen_keys, f"产品 key 重复：{key}")
        seen_keys.add(key)

        logic = str(item.get("logic") or "all").strip().lower()
        _require(logic in ("all", "any"), f"{where}.logic 只能是 all 或 any，收到 {logic!r}")

        try:
            threshold = float(item.get("threshold", 0.75))
        except (TypeError, ValueError) as exc:
            raise RuleDocError(f"{where}.threshold 必须是 0~1 的小数") from exc
        _require(0.0 < threshold <= 1.0, f"{where}.threshold 必须在 (0, 1] 区间")

        rules = item.get("rules")
        _require(isinstance(rules, list) and rules, f"{where}.rules 必须是非空数组")
        clean_rules = []
        for r_index, rule in enumerate(rules):
            r_where = f"{where}.rules[{r_index}]"
            _require(isinstance(rule, dict), f"{r_where} 必须是对象")
            op = str(rule.get("op") or "").strip().lower()
            _require(op in OPS, f"{r_where}.op={op!r} 不支持；可选 {list(OPS)}")
            fname = str(rule.get("field") or "").strip()
            _require(fname in material.KEY_FIELDS,
                     f"{r_where}.field={fname!r} 不是规范字段；"
                     f"可选 {list(material.KEY_FIELDS)}")
            _require("value" in rule or op == "exists",
                     f"{r_where} 缺少 value（exists 除外）")
            try:
                weight = float(rule.get("weight", 1.0))
            except (TypeError, ValueError) as exc:
                raise RuleDocError(f"{r_where}.weight 必须是数字") from exc
            clean_rules.append({
                "id": str(rule.get("id") or f"R{r_index + 1}"),
                "field": fname,
                "op": op,
                "value": rule.get("value"),
                "weight": max(0.0, weight),
                "desc": str(rule.get("desc") or "").strip(),
            })

        required = item.get("required_fields") or []
        _require(isinstance(required, list), f"{where}.required_fields 必须是数组")
        for fname in required:
            _require(str(fname) in material.KEY_FIELDS,
                     f"{where}.required_fields 里的 {fname!r} 不是规范字段")

        parsed.append({
            "product_key": key,
            "product_name": name,
            "version": version,
            "logic": logic,
            "threshold": threshold,
            "rules": clean_rules,
            "required_fields": [str(f) for f in required],
            "note": note,
        })
    return parsed


# --------------------------------------------------------------------------- #
# 导入 / 生效
# --------------------------------------------------------------------------- #
def load_active(db: Session) -> List[ScreeningRule]:
    """当前生效的规则（一个产品一行）。"""
    return (db.query(ScreeningRule)
            .filter(ScreeningRule.status == "ACTIVE")
            .order_by(ScreeningRule.id.asc()).all())


def load_rules(db: Session, status: Optional[str] = None) -> List[ScreeningRule]:
    query = db.query(ScreeningRule)
    if status:
        query = query.filter(ScreeningRule.status == status)
    return query.order_by(ScreeningRule.product_key.asc(), ScreeningRule.id.desc()).all()


def import_rules(db: Session, doc: Dict[str, Any], *,
                 activate: bool = True,
                 imported_by: Optional[str] = None,
                 source_name: Optional[str] = None) -> List[ScreeningRule]:
    """导入一版规则（按 product_key 覆盖式新增，不 commit）。

    `activate=True` 时把该产品此前生效的版本置为 `ARCHIVED` 并让新版本生效 ——
    这就是「规则调整无需发版」的落地方式。
    同一 (product_key, version) 已存在则**覆盖内容**而不是报错，
    方便反复调整同一版草稿。
    """
    parsed = parse_rule_document(doc)
    rows: List[ScreeningRule] = []
    for item in parsed:
        existing = (db.query(ScreeningRule)
                    .filter(ScreeningRule.product_key == item["product_key"],
                            ScreeningRule.version == item["version"]).first())
        if existing is not None:
            row = existing
        else:
            row = ScreeningRule(product_key=item["product_key"],
                                version=item["version"])
            db.add(row)
        row.product_name = item["product_name"]
        row.logic = item["logic"]
        row.threshold = item["threshold"]
        row.rules = item["rules"]
        row.required_fields = item["required_fields"]
        row.note = item["note"] or source_name or None
        row.source_name = source_name or row.source_name
        row.imported_by = imported_by
        row.status = "DRAFT"
        db.flush()
        rows.append(row)

    if activate:
        for row in rows:
            activate_rule(db, row)
    db.flush()
    return rows


def activate_rule(db: Session, row: ScreeningRule) -> ScreeningRule:
    """让某一版规则生效，同产品的旧版本自动 `ARCHIVED`（不 commit）。"""
    others = (db.query(ScreeningRule)
              .filter(ScreeningRule.product_key == row.product_key,
                      ScreeningRule.id != row.id,
                      ScreeningRule.status == "ACTIVE").all())
    for other in others:
        other.status = "ARCHIVED"
    row.status = "ACTIVE"
    db.flush()
    return row


def deactivate_rule(db: Session, row: ScreeningRule) -> ScreeningRule:
    row.status = "ARCHIVED"
    db.flush()
    return row


def rule_snapshot(db: Session) -> Dict[str, Any]:
    """给界面/报告用的「当前生效规则」摘要。"""
    rows = load_active(db)
    return {
        "count": len(rows),
        "versions": {r.product_key: r.version for r in rows},
        "products": [{"key": r.product_key, "name": r.product_name,
                      "version": r.version, "logic": r.logic,
                      "threshold": float(r.threshold or 0.75),
                      "rule_count": len(r.rules or []),
                      "required_fields": list(r.required_fields or []),
                      "source_name": r.source_name, "note": r.note,
                      "status": r.status} for r in rows],
        "source_name": (rows[0].source_name if rows else None),
        "is_sample": bool(rows and (rows[0].source_name or "") == SAMPLE_SOURCE),
    }


# --------------------------------------------------------------------------- #
# 判定
# --------------------------------------------------------------------------- #
def _to_number(value: Any) -> Optional[float]:
    """把字段值转成可比数字。

    现场数据里的形态很杂：`23`、`"23"`、`"3.5/4.0"`、`"85 分"`、`"6.5"`。
    分式取分子（GPA 通常写 3.5/4.0）；抓不到数字返回 None（→ 该规则判为未命中，
    但因为是「取不到值」而不是「明显不符」，会在 evidence 里如实标注）。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    fraction = re.match(r"^\s*(\d+(?:\.\d+)?)\s*/\s*\d+(?:\.\d+)?\s*$", text)
    if fraction:
        return float(fraction.group(1))
    found = re.search(r"\d+(?:\.\d+)?", text)
    return float(found.group(0)) if found else None


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    if value is None:
        return []
    return [value]


def _match_text(actual: Any, expected: Any) -> bool:
    """值域匹配：字符串按「包含」匹配（"本科" ⊂ "本科"、出现于 "本科在读"），
    也支持规则写成 "雅思|IELTS" 的或语义。"""
    a = str(actual).strip().lower()
    for exp in _as_list(expected):
        if exp is None:
            continue
        for piece in str(exp).split("|"):
            piece = piece.strip().lower()
            if piece and piece in a:
                return True
    return False


@dataclass
class RuleHit:
    rule_id: str
    field: str
    label: str
    op: str
    expect: Any
    actual: Any
    hit: bool
    weight: float
    desc: str
    excerpt: Optional[str] = None
    note: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule": self.rule_id, "field": self.field, "label": self.label,
            "op": self.op, "expect": self.expect, "actual": self.actual,
            "hit": self.hit, "weight": self.weight, "desc": self.desc,
            "excerpt": self.excerpt, "note": self.note,
        }


def _excerpt(text: Optional[str], value: Any, span: int = 24) -> Optional[str]:
    """取字段值在原文里的片段（REQ-M1-05「原文片段」）。

    找不到就返回 None —— 编一个片段比不给更糟（那会让「可追溯」变成假象）。
    """
    if not text or value in (None, ""):
        return None
    needle = str(value).strip()
    if not needle:
        return None
    pos = text.find(needle)
    if pos < 0:
        return None
    start = max(0, pos - span)
    end = min(len(text), pos + len(needle) + span)
    snippet = text[start:end].replace("\n", " ").strip()
    return ("…" if start > 0 else "") + snippet + ("…" if end < len(text) else "")


def eval_rule(rule: Dict[str, Any], fields: Dict[str, Any],
              text: Optional[str] = None) -> RuleHit:
    fname = rule["field"]
    label = material.FIELD_LABELS.get(fname, fname)
    op = rule["op"]
    expect = rule.get("value")
    raw = fields.get(fname)
    note = None

    if op == "exists":
        hit = raw not in (None, "", [], {})
    elif raw in (None, "", [], {}):
        # 字段为空：除了 not_in（「不在某集合里」对空值无意义，保守判未命中）
        # 与 ne（空值确实不等于期望值），其余一律未命中且标注「未抽到」
        hit = op in ("ne",)
        note = "该字段未抽取到"
    elif op in ("in", "not_in"):
        base = _match_text(raw, expect)
        hit = base if op == "in" else not base
    elif op == "eq":
        hit = _match_text(raw, expect)
    elif op == "ne":
        hit = not _match_text(raw, expect)
    elif op == "matches":
        try:
            hit = bool(re.search(str(expect), str(raw)))
        except re.error as exc:
            raise RuleDocError(f"规则 {rule.get('id')} 的正则不合法：{exc}") from exc
    else:                                             # gte / lte / between
        actual_num = _to_number(raw)
        if op == "between":
            pair = _as_list(expect)
            if len(pair) != 2:
                raise RuleDocError(f"规则 {rule.get('id')} 的 between 需要 [min, max]")
            low, high = _to_number(pair[0]), _to_number(pair[1])
            if actual_num is None or low is None or high is None:
                hit = False
                note = "数值无法解析，判为未命中"
            else:
                hit = low <= actual_num <= high
        else:
            target = _to_number(expect)
            if actual_num is None or target is None:
                hit = False
                note = "数值无法解析，判为未命中"
            else:
                hit = actual_num >= target if op == "gte" else actual_num <= target

    return RuleHit(rule_id=rule["id"], field=fname, label=label, op=op,
                   expect=expect, actual=raw, hit=hit,
                   weight=float(rule.get("weight", 1.0)),
                   desc=rule.get("desc") or "", excerpt=_excerpt(text, raw),
                   note=note)


def evaluate_product(row: ScreeningRule, fields: Dict[str, Any],
                     text: Optional[str] = None) -> Dict[str, Any]:
    """对**单个产品**判定（REQ-M1-04 的「分别输出」）。"""
    rules = list(row.rules or [])
    required = list(row.required_fields or [])
    threshold = float(row.threshold or 0.75)
    hits = [eval_rule(r, fields, text) for r in rules]

    missing_required = [f for f in required if fields.get(f) in (None, "", [], {})]
    total_weight = sum(h.weight for h in hits) or 0.0
    hit_weight = sum(h.weight for h in hits if h.hit)
    ratio = (hit_weight / total_weight) if total_weight else 0.0

    involved = {h.field for h in hits}
    covered = {f for f in involved if fields.get(f) not in (None, "", [], {})}
    coverage = (len(covered) / len(involved)) if involved else 0.0

    if missing_required:
        conclusion = INSUFFICIENT
    elif row.logic == "any":
        conclusion = CONFORM if hit_weight > 0 else NON_CONFORM
    else:
        conclusion = CONFORM if ratio >= threshold else NON_CONFORM

    confidence = round(0.6 * ratio + 0.4 * coverage, 4)
    # 「信息不足」说明字段不够，此时置信度只反映「已有信息支持度」，不该显得很有把握
    if conclusion == INSUFFICIENT:
        confidence = round(min(confidence, 0.5) * 0.8, 4)

    return {
        "product_key": row.product_key,
        "product_name": row.product_name,
        "version": row.version,
        "logic": row.logic,
        "threshold": threshold,
        "conclusion": conclusion,
        "match": round(ratio, 4),
        "coverage": round(coverage, 4),
        "confidence": confidence,
        "hit_count": sum(1 for h in hits if h.hit),
        "rule_count": len(hits),
        "missing_required": missing_required,
        "missing_labels": [material.FIELD_LABELS.get(f, f) for f in missing_required],
        "evidence": [h.to_dict() for h in hits],
        "reason": _reason(conclusion, hits, missing_required, threshold, row.logic),
    }


def _reason(conclusion: str, hits: Sequence[RuleHit], missing: Sequence[str],
            threshold: float, logic: str) -> str:
    if conclusion == INSUFFICIENT:
        labels = [material.FIELD_LABELS.get(f, f) for f in missing]
        return f"关键字段缺失（{'、'.join(labels)}），按业务规则不判「不符合」，转人工复核"
    failed = [f"{h.rule_id}({h.label})" for h in hits if not h.hit]
    if conclusion == CONFORM:
        if logic == "any":
            return "命中任一必要条件，判定符合"
        return (f"加权满足率 ≥ 阈值 {threshold:.2f}"
                + (f"；未命中：{'、'.join(failed)}" if failed else "；全部规则命中"))
    return ("全部必要条件均未命中" if not failed else f"未命中：{'、'.join(failed)}")


def evaluate(rules: Iterable[ScreeningRule], fields: Dict[str, Any],
             text: Optional[str] = None) -> Dict[str, Any]:
    """对全部生效规则做一次完整研判（REQ-M1-04 / REQ-M1-05）。"""
    rows = list(rules)
    products = [evaluate_product(row, fields, text) for row in rows]

    conform = [p for p in products if p["conclusion"] == CONFORM]
    insufficient = [p for p in products if p["conclusion"] == INSUFFICIENT]

    if conform:
        conclusion = CONFORM
    elif insufficient:
        conclusion = INSUFFICIENT
    else:
        conclusion = NON_CONFORM

    evidence: List[Dict[str, Any]] = []
    for product in products:
        for item in product["evidence"]:
            evidence.append({"product": product["product_name"],
                             "product_key": product["product_key"],
                             "rule_version": product["version"], **item})

    # 整体置信度取「支持当前整体结论的那些产品」的最大值：
    # 判「符合」时看命中的产品，判「信息不足」时看缺字段的产品，
    # 都判「不符合」时取最高满足率（代表「最接近也没够上」）。
    pool = conform or insufficient or products
    confidence = max((p["confidence"] for p in pool), default=0.0)

    return {
        "conclusion": conclusion,
        "hit_products": [{"key": p["product_key"], "name": p["product_name"],
                          "match": p["match"], "confidence": p["confidence"]}
                         for p in conform],
        "products": products,
        "evidence": evidence,
        "confidence": round(confidence, 4),
        "rule_versions": {p["product_key"]: p["version"] for p in products},
        "rule_source": "local",
    }


# --------------------------------------------------------------------------- #
# 随包示例规则（等甲方《用户画像研判规则》到位后整体替换）
# --------------------------------------------------------------------------- #
def default_rule_document() -> Dict[str, Any]:
    """一份**示例**规则文档，只为了让引擎跑起来、让接口有东西可校验。

    ⚠️ 这不是甲方口径。`source_name` 会写成 `SAMPLE_SOURCE`，
    研判响应带 `rule_note`，界面也会标「示例规则」。
    两个产品的划分（硕士直申 / 语言培训）取自客户需求表对「两个产品」的
    业务语境描述，具体阈值**必须由甲方确认**（SRS 待确认事项第 2 条）。

    规则字段一律使用 `material.extract_fields` 的规范键名
    （name / age / degree / school / major / language / gpa /
    intention_country / intention_stage），保证抽取与判定口径一致 ——
    早期版本写了抽取器不产出的字段，会永远判「未命中」，属于规则本身的 bug。

    `required_fields` 只放「缺了就完全无法判断」的字段（用来产出「信息不足」），
    其余字段按权重参与打分：缺失只是拿不到分、拉低满足率，而不会误判「不符合」。
    """
    return {
        "version": "V1.0-sample",
        "note": "随包示例规则，用于打通引擎与接口；待甲方《用户画像研判规则》到位后替换",
        "products": [
            {
                "key": "postgrad_direct",
                "name": "硕士直申",
                "logic": "all",
                "threshold": 0.6,
                # 学历是这类需求唯一的硬前提；语言成绩属加分项，缺了不该直接转人工
                "required_fields": ["degree"],
                "rules": [
                    {"id": "PG-1", "field": "degree", "op": "in",
                     "value": ["本科", "硕士", "在读", "学士", "大专"],
                     "weight": 1.0, "desc": "学历为本科及以上（含在读）"},
                    {"id": "PG-2", "field": "gpa", "op": "gte",
                     "value": 2.5, "weight": 1.0,
                     "desc": "均分/GPA 不低于 2.5（4 分制）或 75（百分制口径待甲方确认）"},
                    {"id": "PG-3", "field": "intention_country", "op": "exists",
                     "weight": 0.8, "desc": "有明确意向国家"},
                    {"id": "PG-4", "field": "language", "op": "matches",
                     "value": "雅思|IELTS|托福|TOEFL|PTE|多邻国|Duolingo|日语|JLPT|德语|法语",
                     "weight": 0.6, "desc": "有语言考试成绩（加分项）"},
                    {"id": "PG-5", "field": "intention_stage", "op": "in",
                     "value": ["硕士", "研究生", "博士", "本科", "高中", "预科", "语言"],
                     "weight": 0.4, "desc": "意向就读阶段明确"},
                ],
            },
            {
                "key": "language_training",
                "name": "语言培训",
                "logic": "all",
                "threshold": 0.6,
                # 语言培训必须知道「考什么」，这是硬前提
                "required_fields": ["language"],
                "rules": [
                    {"id": "LT-1", "field": "language", "op": "matches",
                     "value": "雅思|IELTS|托福|TOEFL|PTE|多邻国|Duolingo|日语|JLPT|德语|法语",
                     "weight": 1.0, "desc": "有语言考试意向或成绩"},
                    {"id": "LT-2", "field": "intention_country", "op": "exists",
                     "weight": 0.8, "desc": "有明确意向国家/地区"},
                    {"id": "LT-3", "field": "degree", "op": "exists",
                     "weight": 0.6, "desc": "有学历背景信息"},
                ],
            },
        ],
    }


def seed_default_rules(db: Session) -> List[ScreeningRule]:
    """把示例规则灌进去（幂等：已存在同版本就复用并启用）。"""
    return import_rules(db, default_rule_document(), activate=True,
                        imported_by="system", source_name=SAMPLE_SOURCE)
