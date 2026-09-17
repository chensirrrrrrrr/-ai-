# -*- coding: utf-8 -*-
"""敏感字段级加密：手机号「打码展示 + 密文存储 + HMAC 检索」三件套。

为什么是这个组合
----------------
手机号在业务里有三种互相冲突的诉求：

1. **展示**：界面只该看到 `137****5678` —— 谁都不需要看全号；
2. **存储**：库里不该有明文（拖库/备份泄露是 PII 事故的头号来源）→ Fernet 密文；
3. **检索**：查重、按号查人要求**等值匹配**，而 Fernet 带随机 nonce，
   同一个号两次加密结果不同 → 再存一列 HMAC-SHA256（密钥化、确定性、可索引）。

`phone` 列保留并改存**打码值**（而非删列）：API 的输出结构、前端展示、
 LIKE 检索全部不用动 —— 这是「加密而不大改」的关键取舍。

密钥与开关
----------
- `FIELD_ENCRYPTION_KEY` 为空 ⇒ **关闭**（完全兼容旧行为，明文进明文出）；
- 配置 Fernet key ⇒ 开启。key 丢失 = 数据不可恢复，换 key 前先解密导出。

适用范围：客户手机号（`customer_lead`）。员工手机号是内部通讯录，本来就要被
同事看见，不在本模块范围内（要收也一样套 `apply_phone`）。
"""
from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session

from ..config import settings
from ..models import CustomerLead

_PHONE_NORMALIZE_RE = re.compile(r"[\s\-]")


def enabled() -> bool:
    """是否开启字段加密（读 settings，运行时可切，便于测试）。"""
    return bool(settings.field_encryption_key.strip())


def normalize_phone(value: str) -> str:
    """去掉空格 / 连字符，统一比较口径。"""
    return _PHONE_NORMALIZE_RE.sub("", str(value or "").strip())


def mask_phone(value: str) -> str:
    """`13712345678` → `137****5678`；过短的值整体打码（别让它漏出结构）。"""
    digits = normalize_phone(value)
    if len(digits) < 7:
        return "*" * len(digits)
    return f"{digits[:3]}{'*' * (len(digits) - 7)}{digits[-4:]}"


def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(settings.field_encryption_key.strip().encode("utf-8"))


def phone_hash(value: str) -> str:
    """确定性 HMAC（key=settings.field_encryption_key），供等值检索与查重。"""
    digest = hmac.new(settings.field_encryption_key.strip().encode("utf-8"),
                      f"phone:{normalize_phone(value)}".encode("utf-8"),
                      hashlib.sha256).hexdigest()
    return digest


def encrypt(value: str) -> str:
    return _fernet().encrypt(normalize_phone(value).encode("utf-8")).decode("utf-8")


def decrypt(token: str) -> str:
    """解密（密文被篡改 / key 不对会抛 InvalidToken —— 让它炸，别吞）。"""
    return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")


def display(value: str) -> str:
    """对外展示口径：开启时打码，关闭时原样（审计/日志里也用它，别落全号）。"""
    return mask_phone(value) if enabled() else value


def apply_phone(plain: str) -> Tuple[str, Optional[str], Optional[str]]:
    """把一个明文手机号折算成要落库的三元组 `(展示值, hash, 密文)`。

    关闭时退化为 `(明文, None, None)` —— 与历史行为完全一致。
    """
    if not enabled():
        return plain, None, None
    return mask_phone(plain), phone_hash(plain), encrypt(plain)


def lookup_condition(plain: str):
    """按手机号等值检索的条件：兼容「密文行 + 历史明文行」并存期。

    开启时 `phone_hash == HMAC(...)`；关闭时退化为 `phone == 明文`。
    返回 SQLAlchemy 可直接放进 `filter()` 的表达式（调用方包 or_ 处理并存）。
    """
    from ..models import CustomerLead
    if not enabled():
        return CustomerLead.phone == plain
    return CustomerLead.phone_hash == phone_hash(plain)


def backfill_phone_encryption(db: Session, *, batch_size: int = 500) -> int:
    """把存量**明文**手机号行回填成「打码 + 密文 + HMAC」（幂等，跳过已回填行）。

    ⚠️ 只在开启 `FIELD_ENCRYPTION_KEY` 后调用；跑之前先备份数据库 ——
    打码会覆盖 `phone` 列的明文，key 丢了就再也回不去。
    """
    if not enabled():
        return 0
    rows = (db.query(CustomerLead)
            .filter(CustomerLead.phone_hash.is_(None)).limit(batch_size).all())
    count = 0
    for row in rows:
        plain = row.phone
        # 打码值里带 *（历史上手工处理过 / 已经回填过）—— 不能当成明文再处理一次，
        # 否则 mask(mask(x)) 会把号彻底打碎
        if not plain or "*" in plain:
            continue
        row.phone, row.phone_hash, row.phone_enc = apply_phone(plain)
        count += 1
    db.commit()
    return count
