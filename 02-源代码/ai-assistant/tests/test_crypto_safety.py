# -*- coding: utf-8 -*-
"""敏感字段加密 + 内容安全过滤。

钉住的关键点
------------
- **加密三件套的口径**：`phone` 列存打码值、`phone_enc` 是可解密回全号的密文、
  `phone_hash` 是确定性 HMAC —— 三者缺一不可（少 hash 就没法查重，少打码就漏 PII）；
- **查重与检索在加密开启后必须照常工作**（这是「加密不能牺牲业务」的底线），
  且要兼容「密文行 + 历史明文行」并存的过渡期；
- **内容安全**：PII 打码（放行）与违禁拦截（拒绝）是两条不同的路，不能混。
"""
from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings
from app.db import SessionLocal
from app.models import CustomerLead
from app.services import agent_tools, content_safety, crypto
from tests.conftest import data

KEY = Fernet.generate_key().decode()


@pytest.fixture
def enc_on(monkeypatch):
    monkeypatch.setattr(settings, "field_encryption_key", KEY)
    yield


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _phone() -> str:
    return f"138{uuid.uuid4().int % 10**8:08d}"


# --------------------------------------------------------------------------- #
# crypto 服务层
# --------------------------------------------------------------------------- #
def test_mask_phone_format():
    assert crypto.mask_phone("13712345678") == "137****5678"
    assert crypto.mask_phone("137 1234-5678") == "137****5678"     # 先归一再打码
    assert crypto.mask_phone("12345") == "*****"                    # 过短整体打码


def test_phone_hash_is_deterministic_and_not_reversible():
    assert crypto.phone_hash("13712345678") == crypto.phone_hash("137 1234-5678")
    assert len(crypto.phone_hash("13712345678")) == 64
    assert "13712345678" not in crypto.phone_hash("13712345678")


def test_encrypt_roundtrip_and_tamper_detection(enc_on):
    token = crypto.encrypt("13712345678")
    assert "13712345678" not in token and "137" not in token
    assert crypto.decrypt(token) == "13712345678"
    with pytest.raises(InvalidToken):
        crypto.decrypt(token[:-4] + "AAAA")                         # 篡改必须炸


def test_disabled_is_fully_backward_compatible():
    """key 为空 = 关闭：明文进出、不写 hash/enc，与历史行为逐字节一致。"""
    assert crypto.enabled() is False
    assert crypto.apply_phone("13712345678") == ("13712345678", None, None)
    assert crypto.display("13712345678") == "13712345678"


def test_display_masks_when_enabled(enc_on):
    assert crypto.display("13712345678") == "137****5678"


# --------------------------------------------------------------------------- #
# API 层：加密下的建客 / 查重 / 详情 / 工具检索
# --------------------------------------------------------------------------- #
def test_lead_create_stores_ciphertext_not_plaintext(client, advisor_headers, enc_on, db):
    phone = _phone()
    created = data(client.post("/api/v1/leads", headers=advisor_headers,
                               json={"name": "加密客户", "phone": phone}))

    row = db.get(CustomerLead, created["id"])
    assert row.phone == crypto.mask_phone(phone)                    # 展示列是打码值
    assert row.phone_hash == crypto.phone_hash(phone)               # 检索列是 HMAC
    assert row.phone_enc and crypto.decrypt(row.phone_enc) == phone  # 密文可解回
    assert phone not in (row.phone_enc or "") and phone not in row.phone

    detail = data(client.get(f"/api/v1/leads/{created['id']}", headers=advisor_headers))
    assert detail["lead"]["phone"] == crypto.mask_phone(phone)       # 接口输出打码


def test_duplicate_detection_still_works_when_encrypted(client, advisor_headers, enc_on):
    phone = _phone()
    data(client.post("/api/v1/leads", headers=advisor_headers,
                     json={"name": "查重甲", "phone": phone}))
    dup = client.post("/api/v1/leads", headers=advisor_headers,
                      json={"name": "查重乙", "phone": phone})
    assert dup.status_code == 409


def test_duplicate_detection_catches_legacy_plaintext_row(client, advisor_headers, monkeypatch):
    """并存期：先有关闭加密时的明文行，再开启加密建同号 —— 也必须拦住。"""
    phone = _phone()
    data(client.post("/api/v1/leads", headers=advisor_headers,
                     json={"name": "历史明文客户", "phone": phone}))   # 此时 key 为空

    monkeypatch.setattr(settings, "field_encryption_key", KEY)          # 开启加密
    dup = client.post("/api/v1/leads", headers=advisor_headers,
                      json={"name": "后来客户", "phone": phone})
    assert dup.status_code == 409


def test_agent_tool_finds_lead_by_phone_when_encrypted(client, advisor_headers, enc_on, db):
    phone = _phone()
    lead_id = data(client.post("/api/v1/leads", headers=advisor_headers,
                               json={"name": "工具检索客户", "phone": phone}))["id"]
    ctx = agent_tools.ToolContext(actor_subject="t", actor_role="admin")
    handler = agent_tools.TOOL_REGISTRY["lead_query"].handler

    by_phone = handler(db, {"phone": phone}, ctx)                       # 等值（HMAC）
    assert [x["id"] for x in by_phone["leads"]] == [lead_id]
    assert by_phone["leads"][0]["phone"] == crypto.mask_phone(phone)

    by_keyword = handler(db, {"keyword": phone}, ctx)                   # 纯数字关键字兜底
    assert [x["id"] for x in by_keyword["leads"]] == [lead_id]


def test_agent_tool_lead_create_encrypts(client, enc_on, db, ctx=None):
    ctx = agent_tools.ToolContext(actor_subject="t", actor_role="admin")
    phone = _phone()
    out = agent_tools.TOOL_REGISTRY["lead_create"].handler(
        db, {"name": "工具建客", "phone": phone}, ctx)
    assert out["phone"] == crypto.mask_phone(phone)
    row = db.get(CustomerLead, out["id"])
    assert row.phone_hash == crypto.phone_hash(phone)
    assert crypto.decrypt(row.phone_enc) == phone


# --------------------------------------------------------------------------- #
# 存量回填
# --------------------------------------------------------------------------- #
def test_backfill_encrypts_legacy_rows_and_is_idempotent(client, advisor_headers, monkeypatch, db):
    phone = _phone()
    legacy_id = data(client.post("/api/v1/leads", headers=advisor_headers,
                                 json={"name": "回填客户", "phone": phone}))["id"]  # key 为空 → 明文

    monkeypatch.setattr(settings, "field_encryption_key", KEY)
    assert crypto.backfill_phone_encryption(db) >= 1                     # 至少回填了这条

    row = db.get(CustomerLead, legacy_id)
    assert row.phone == crypto.mask_phone(phone)
    assert row.phone_hash == crypto.phone_hash(phone)
    assert crypto.decrypt(row.phone_enc) == phone

    assert crypto.backfill_phone_encryption(db) == 0                     # 幂等：再跑为 0


def test_backfill_refuses_when_disabled():
    assert crypto.backfill_phone_encryption(SessionLocal()) == 0         # 关闭时不做任何事


def test_lookup_condition_falls_back_to_plaintext_equality():
    """关闭加密时检索条件必须退化为普通等值匹配（老行为）。"""
    cond = crypto.lookup_condition("13712345678")
    assert str(cond).find("customer_lead.phone") >= 0 and "phone_hash" not in str(cond)


def test_backfill_skips_masked_rows(client, advisor_headers, monkeypatch, db):
    """已经打码（历史手工处理过）的行不能被再次「回填」——跳过且不破坏。"""
    phone = _phone()
    masked = crypto.mask_phone(phone)
    row = CustomerLead(name="已打码客户", phone=masked, status="NEW")
    db.add(row)
    db.commit()
    monkeypatch.setattr(settings, "field_encryption_key", KEY)
    crypto.backfill_phone_encryption(db)
    db.expire_all()
    after = db.get(CustomerLead, row.id)
    assert after.phone == masked and after.phone_hash is None            # 原样保留


# --------------------------------------------------------------------------- #
# 内容安全
# --------------------------------------------------------------------------- #
def test_sanitize_masks_pii_and_keeps_clean_text():
    text = ("家长张三，手机 13812345678，身份证 110101199003070012，"
            "银行卡 6222020200112233445，孩子想申请英国硕士。")
    clean, findings = content_safety.sanitize(text)
    assert "13812345678" not in clean and "138****5678" in clean
    assert "110101199003070012" not in clean and clean.count("*") >= 8
    assert "6222020200112233445" not in clean
    assert set(findings) == {"phone", "id_card", "bank_card"}
    assert "英国硕士" in clean                                          # 业务内容不动

    clean2, findings2 = content_safety.sanitize("我的成绩怎么样")
    assert clean2 == "我的成绩怎么样" and findings2 == []


def test_blocked_words_detection():
    assert content_safety.is_blocked("帮我搞点枪支")
    assert not content_safety.is_blocked("我想咨询留学申请")


def test_chat_refuses_blocked_message_without_hitting_model(client, student_headers):
    body = data(client.post("/api/v1/chat/message",
                            json={"message": "哪里能买到枪支", "session_id": "guard"},
                            headers=student_headers))
    assert "敏感内容" in body["answer"]
    assert body["agent"] == "guard" and body["intent"] == "content_blocked"


def test_chat_masks_phone_before_it_reaches_agent_and_audit(client, student_headers, db):
    """消息里的手机号打码后才进模型/审计 —— 审计库里不能捞到明文全号。"""
    from app.models import AuditLog

    phone = _phone()
    data(client.post("/api/v1/chat/message",
                     json={"message": f"我手机号是{phone}，帮我看看进度",
                           "session_id": "pii"},
                     headers=student_headers))
    records = (db.query(AuditLog)
               .filter(AuditLog.actor_id == "student")
               .order_by(AuditLog.id.desc()).limit(10).all())
    assert records, "对话应产生审计记录"
    assert all(phone not in str(r.detail) for r in records)


def test_mask_helper_short_value():
    """过短的号段整体打码，不保留任何结构。"""
    assert content_safety._mask("1234", 6, 4) == "****"
    assert content_safety._mask("12345678901234", 0, 4) == "**********1234"
