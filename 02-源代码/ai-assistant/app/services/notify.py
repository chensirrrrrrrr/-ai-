"""统一通知 / 触达服务。

解决的问题
----------
「系统主动告知某人」在本项目里有六处：报告推送、工单解决通知、请假审批结果、
DDL 考前提醒、增值转化推荐、心理预警触达。如果每处都往自己的表里写一列
`notified_at`，就会得到六套半成品 —— 有的只记时间不记内容、有的记了但界面看不到、
有的根本没有「读没读」。所以统一成一张 `notification` 表 + 一个 `send()` 入口。

通道与降级（重要）
------------------
- `internal`（站内）：**落库即达**，前端「我的通知」能看到，永远可用，是兜底通道；
- `wecom` / `sms` / `webhook`：真实外发，**需要凭证**。本项目当前没有企业微信
  应用凭证、也没有短信服务商账号，所以这些通道在未配置时**降级为站内**。

降级必须留下痕迹，不能让「已触达」变成一句无法验证的话 ——
`channel` 记「请求的通道」，`delivered_via` 记「**实际生效的通道**」，
`detail` 里写清楚为什么降级/失败。这与 `services/material.py` 的 `parser`
字段同一口径：**不静默降级**。

外发失败也一样：先记 `detail`，然后**兜底落站内**（降级优先，见 TDD 2.2），
保证人总能在「我的通知」里看到这件事，而不是石沉大海。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import NOTIFY_CHANNELS, settings
from ..models import Notification

logger = logging.getLogger(__name__)

INTERNAL = "internal"

# 类别 → 中文名（界面与文案复用，避免各处自己写一份）
CATEGORY_LABELS: Dict[str, str] = {
    "report": "业务报告",
    "ticket": "售后工单",
    "leave": "行政申请",
    "deadline": "学业节点",
    "promotion": "升学推荐",
    "alert": "心理预警",
    "screening": "客户研判",
    "system": "系统消息",
}


def category_label(category: str) -> str:
    return CATEGORY_LABELS.get(category, category or "通知")


# --------------------------------------------------------------------------- #
# 通道
# --------------------------------------------------------------------------- #
def channel_ready(channel: str) -> bool:
    """通道是否**真的**能发出去（未配置凭证的通道不算就绪）。"""
    return settings.notify_channel_ready(channel)


def resolve_channel(requested: Optional[str]) -> Tuple[str, str]:
    """返回 (请求的通道, 实际生效的通道)。两者不同即代表发生了降级。"""
    name = (requested or settings.notify_default_channel or INTERNAL).strip().lower()
    if name not in NOTIFY_CHANNELS:
        name = INTERNAL
    if channel_ready(name):
        return name, name
    return name, INTERNAL


def _post_json(url: str, payload: Dict[str, Any]) -> Tuple[bool, str]:
    """POST 一个 JSON。**同步 + 短超时**，失败只返回原因不抛。

    为什么同步：调用方是定时线程与请求线程，两种场景都能接受几百毫秒；
    异步化会把 notify 变成 async，进而逼着所有写业务的地方一起 async ——
    为了一个「尽力而发」的通知不值当。
    """
    try:
        # 本机/内网目标绕开系统代理，否则会被 HTTP_PROXY 接走（本机实测踩过）
        trust = settings.resolve_proxy_trust(url)
        with httpx.Client(timeout=settings.notify_http_timeout, trust_env=trust) as client:
            resp = client.post(url, json=payload)
        if 200 <= resp.status_code < 300:
            return True, f"HTTP {resp.status_code}"
        return False, f"HTTP {resp.status_code} {resp.text[:120]}"
    except Exception as exc:                                 # noqa: BLE001 - 尽力而发
        return False, f"{type(exc).__name__}: {exc}"


def _deliver(channel: str, title: str, body: Optional[str]) -> Tuple[bool, Dict[str, Any]]:
    """真实外发。返回 (是否成功, 明细)。"""
    text = title if not body else f"{title}\n{body}"
    if channel == "wecom":
        # 企业微信群机器人：{"msgtype":"text","text":{"content":"..."}}
        ok, info = _post_json(settings.notify_wecom_webhook.strip(),
                              {"msgtype": "text", "text": {"content": text}})
        return ok, {"target": "wecom_webhook", "info": info}
    if channel == "webhook":
        ok, info = _post_json(settings.notify_webhook_url.strip(),
                              {"title": title, "body": body or "", "ts": datetime.now().isoformat()})
        return ok, {"target": "webhook", "info": info}
    if channel == "sms":
        # 短信适配器待接（需要服务商 SDK / 签名 / 模板报备），此处只做「未接入」的诚实记录
        return False, {"target": "sms", "info": "短信适配器未接入（需要服务商账号与模板报备）"}
    return True, {"target": channel}


# --------------------------------------------------------------------------- #
# 发送
# --------------------------------------------------------------------------- #
def send(db: Session, *,
         recipient_type: str,
         recipient_id: int,
         category: str,
         title: str,
         body: Optional[str] = None,
         recipient_subject: Optional[str] = None,
         biz_type: Optional[str] = None,
         biz_id: Optional[Any] = None,
         channel: Optional[str] = None) -> Notification:
    """发一条通知（不 commit，由调用方决定事务边界）。

    `recipient_type`：`student` / `employee`。
    `channel` 不传 = 走 `NOTIFY_DEFAULT_CHANNEL`（默认站内）。
    """
    title = (title or "").strip()[:settings.notify_title_max] or "系统通知"
    requested, actual = resolve_channel(channel)

    detail: Dict[str, Any] = {}
    if requested != actual:
        detail["degraded"] = True
        detail["reason"] = f"{requested} 通道未配置凭证，已降级为站内"
    elif actual != INTERNAL:
        ok, info = _deliver(actual, title, body)
        detail["attempt"] = info
        if not ok:
            # 外发失败 → 兜底落站内（降级优先），但把失败原因留在 detail 里
            detail["degraded"] = True
            detail["reason"] = f"{actual} 外发失败，已降级为站内"
            actual = INTERNAL

    row = Notification(
        recipient_type=recipient_type, recipient_id=recipient_id,
        recipient_subject=recipient_subject, category=category,
        title=title, body=body,
        biz_type=biz_type, biz_id=str(biz_id) if biz_id is not None else None,
        channel=requested, delivered_via=actual,
        status="SENT", detail=detail or None,
    )
    db.add(row)
    db.flush()
    return row


def send_many(db: Session, recipients: List[Dict[str, Any]], *,
              category: str, title: str, body: Optional[str] = None,
              biz_type: Optional[str] = None, biz_id: Optional[Any] = None,
              channel: Optional[str] = None) -> List[Notification]:
    """群发同一内容（报告推送、批量提醒用）。`recipients` 是 `send()` 的 kwargs 列表。"""
    return [send(db, category=category, title=title, body=body, biz_type=biz_type,
                 biz_id=biz_id, channel=channel, **item) for item in recipients]


# --------------------------------------------------------------------------- #
# 查询 / 已读
# --------------------------------------------------------------------------- #
def list_for(db: Session, *, recipient_type: str, recipient_id: int,
             category: Optional[str] = None, unread_only: bool = False,
             limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    query = db.query(Notification).filter(
        Notification.recipient_type == recipient_type,
        Notification.recipient_id == recipient_id,
    )
    if category:
        query = query.filter(Notification.category == category)
    if unread_only:
        query = query.filter(Notification.read_at.is_(None))

    total = query.count()
    rows = (query.order_by(Notification.id.desc())
            .offset(offset).limit(max(1, min(limit, 200))).all())
    unread = unread_count(db, recipient_type=recipient_type, recipient_id=recipient_id)
    return {"total": total, "unread": unread, "items": [dump(r) for r in rows]}


def unread_count(db: Session, *, recipient_type: str, recipient_id: int) -> int:
    return int(db.query(func.count(Notification.id)).filter(
        Notification.recipient_type == recipient_type,
        Notification.recipient_id == recipient_id,
        Notification.read_at.is_(None),
    ).scalar() or 0)


def mark_read(db: Session, notif_id: int, *, recipient_type: str, recipient_id: int,
              read: bool = True) -> Optional[Notification]:
    """标记已读/未读。**幂等**：重复标记不刷新时间（与 todo 的 ack 同一口径）。"""
    row = db.get(Notification, notif_id)
    if row is None or row.recipient_type != recipient_type or row.recipient_id != recipient_id:
        return None
    if read and row.read_at is None:
        row.read_at = datetime.now()
    elif not read:
        row.read_at = None
    db.flush()
    return row


def mark_all_read(db: Session, *, recipient_type: str, recipient_id: int) -> int:
    rows = db.query(Notification).filter(
        Notification.recipient_type == recipient_type,
        Notification.recipient_id == recipient_id,
        Notification.read_at.is_(None),
    ).all()
    now = datetime.now()
    for row in rows:
        row.read_at = now
    db.flush()
    return len(rows)


def dump(row: Notification) -> Dict[str, Any]:
    return {
        "id": row.id,
        "category": row.category,
        "category_label": category_label(row.category),
        "title": row.title,
        "body": row.body,
        "biz_type": row.biz_type,
        "biz_id": row.biz_id,
        "channel": row.channel,
        "delivered_via": row.delivered_via,
        "degraded": bool((row.detail or {}).get("degraded")),
        "detail": row.detail,
        "status": row.status,
        "delivered_at": row.delivered_at.isoformat(timespec="seconds") if row.delivered_at else None,
        "read_at": row.read_at.isoformat(timespec="seconds") if row.read_at else None,
    }


def channel_status() -> Dict[str, Any]:
    """各通道就绪情况（界面提示 / 就绪探针用）。"""
    return {
        "default": settings.notify_default_channel,
        "ready": settings.notify_ready_channels,
        "channels": [
            {"name": c, "ready": channel_ready(c),
             "note": "" if channel_ready(c) else "未配置凭证，将降级为站内"}
            for c in NOTIFY_CHANNELS
        ],
    }
