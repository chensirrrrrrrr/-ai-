# -*- coding: utf-8 -*-
"""存量明文客户手机号 → 加密存储的一次性回填。

用法（先备份数据库！）：
    set FIELD_ENCRYPTION_KEY=<Fernet key>
    python scripts/encrypt_backfill.py [--dry-run]

行为：`customer_lead` 里 `phone_hash` 为空且 `phone` 看着是明文的行，
改写为「phone=打码值 + phone_hash=HMAC + phone_enc=Fernet 密文」。
幂等：已回填（phone_hash 非空）或已打码的行自动跳过。
key 丢失 = 密文不可恢复，务必先备份、再把 key 放进密钥管理而不是代码库。
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import settings          # noqa: E402
from app.db import SessionLocal          # noqa: E402
from app.models import CustomerLead      # noqa: E402
from app.services import crypto          # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="客户手机号加密回填")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = parser.parse_args()

    if not crypto.enabled():
        print("未配置 FIELD_ENCRYPTION_KEY，加密处于关闭状态 —— 没有需要回填的内容。")
        return

    db = SessionLocal()
    try:
        rows = db.query(CustomerLead).filter(CustomerLead.phone_hash.is_(None)).all()
        targets = [r for r in rows if r.phone and not r.phone.startswith("*")]
        print(f"待回填 {len(targets)} 行（跳过已回填 {len(rows) - len(targets)} 行）。")
        if args.dry_run:
            for row in targets[:10]:
                print(f"  - #{row.id} {row.name} {crypto.mask_phone(row.phone)}")
            if len(targets) > 10:
                print(f"  ... 共 {len(targets)} 行")
            return
        changed = crypto.backfill_phone_encryption(db, batch_size=max(len(targets), 1))
        print(f"已回填 {changed} 行。")
    finally:
        db.close()


if __name__ == "__main__":
    main()
