"""统一通知 / 触达（P0-3）：通道降级、报告定时推送、通知中心已读闭环。

覆盖点（对应查验报告里的 P0-3 缺口）：
1. 未配置凭证的外发通道**降级为站内**，且 `delivered_via` 如实标出实际通道
   （不静默降级 —— 与 `services/material.py` 的 `parser` 字段同一口径）；
2. 报告定时生成后**顺手推送**给目标角色（SRS 4.5.3 / AC-08）；
3. 推送幂等：同一报告对同一账号只推一次；
4. 通知中心：只看得到自己的、已读/全部已读闭环、访客没有收件箱。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.db import SessionLocal
from app.services import notify, reports
from tests.conftest import data


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# 通道就绪与降级
# --------------------------------------------------------------------------- #
def test_channels_reveal_degradation(client, manager_headers):
    body = data(client.get("/api/v1/notifications/channels", headers=manager_headers))
    assert body["default"] in body["ready"] or body["default"] == "internal"
    by_name = {c["name"]: c for c in body["channels"]}
    assert set(by_name) == {"internal", "wecom", "sms", "webhook"}
    # 站内永远可用（兜底通道）
    assert by_name["internal"]["ready"] is True
    # 测试环境没有企业微信/短信凭证 → 必须如实报「未就绪」并说明会降级
    for name in ("wecom", "sms", "webhook"):
        if not by_name[name]["ready"]:
            assert "降级" in by_name[name]["note"]


def test_send_degrades_to_internal_and_records_it():
    """直接调服务层：请求企业微信但没配凭证 → 落站内，且 detail 记降级原因。"""
    from app.db import SessionLocal
    from app.services import notify as svc

    db = SessionLocal()
    try:
        row = svc.send(db, recipient_type="employee", recipient_id=9999,
                       category="system", title="降级验证",
                       channel="wecom")
        assert row.channel == "wecom"                 # 请求的通道
        assert row.delivered_via == "internal"        # 实际生效的通道
        assert (row.detail or {}).get("degraded") is True
        assert "未配置凭证" in (row.detail or {}).get("reason", "")
        db.rollback()                                 # 不污染共享库
    finally:
        db.close()


def test_resolve_channel_falls_back_on_unknown_name():
    assert notify.resolve_channel("telepathy") == ("internal", "internal")
    assert notify.resolve_channel(None)[1] == "internal"


# --------------------------------------------------------------------------- #
# 报告定时推送（AC-08）
#
# ⚠️ 这里**刻意不复用 `/reports/scheduled/run` 的「今天」报告期** —— 那会和
#    test_reports.py 里 `created_count == 5` 的断言抢同一批定时报告，
#    造成「按文件字母序执行才通过」的假绿。改用服务层 + 一个远期报告期，
#    自己造自己的数据。API 层的「定时即推送」由 test_reports.py 覆盖。
# --------------------------------------------------------------------------- #
def test_run_scheduled_with_push_creates_and_notifies(db):
    from app.models import Notification, ReportRecord

    moment = datetime(2027, 3, 1, 9, 0)                 # 远期周一，不与任何用例撞期
    result = reports.run_scheduled_with_push(db, now=moment, force=True)
    db.commit()

    assert result["created_count"] == 5
    assert len(result["pushed"]) == 5
    assert result["pushed_count"] == sum(d["sent"] for d in result["pushed"])
    assert result["pushed_count"] >= 1, "定时生成后必须有人收到推送"

    ids = [item["id"] for item in result["created"]]
    rows = (db.query(ReportRecord)
            .filter(ReportRecord.id.in_(ids),
                    ReportRecord.from_schedule.is_(True)).all())
    assert len(rows) == 5

    notices = (db.query(Notification)
               .filter(Notification.category == "report",
                       Notification.biz_type == "report_record",
                       Notification.biz_id.in_([str(i) for i in ids])).all())
    assert notices, "推送必须落到统一通知表，前端「我的通知」才看得到"
    assert all(n.delivered_via == "internal" for n in notices)
    assert all(n.channel == "internal" for n in notices)

    # 再跑一次：报告不重建（幂等），推送也不重发
    second = reports.run_scheduled_with_push(db, now=moment, force=True)
    db.commit()
    assert second["created_count"] == 0 and second["pushed_count"] == 0


def test_report_push_is_idempotent(client, manager_headers):
    created = data(client.post("/api/v1/reports/generate",
                               json={"report_type": "weekly_digest", "title": "幂等推送验证周报"},
                               headers=manager_headers))
    rid = created["id"]

    first = data(client.post(f"/api/v1/reports/{rid}/push",
                             json={"roles": ["manager"]}, headers=manager_headers))
    assert first["sent"] >= 1 and first["skipped"] == 0

    second = data(client.post(f"/api/v1/reports/{rid}/push",
                              json={"roles": ["manager"]}, headers=manager_headers))
    assert second["sent"] == 0 and second["skipped"] >= 1, "同一报告对同一账号不应重复推送"

    deliveries = data(client.get(f"/api/v1/reports/{rid}/deliveries",
                                 headers=manager_headers))
    assert deliveries["total"] == first["sent"]
    # 默认目标角色口径写死在 services/reports.REPORT_TARGET_ROLES
    assert set(deliveries["targets"]) <= {"manager", "admin"}


def test_push_to_unknown_role_sends_nothing(client, manager_headers):
    created = data(client.post("/api/v1/reports/generate",
                               json={"report_type": "daily_digest"}, headers=manager_headers))
    body = data(client.post(f"/api/v1/reports/{created['id']}/push",
                            json={"roles": ["no_such_role"]}, headers=manager_headers))
    assert body["account_count"] == 0 and body["sent"] == 0        # 不报错，但如实说明没人可推


def test_report_push_reports_delivered_channel(client, manager_headers):
    """显式请求企业微信（未配凭证）→ 报告推送必须降级为站内并如实标注。"""
    created = data(client.post("/api/v1/reports/generate",
                               json={"report_type": "daily_digest", "title": "降级通道验证日报"},
                               headers=manager_headers))
    body = data(client.post(f"/api/v1/reports/{created['id']}/push",
                            json={"roles": ["manager"], "channel": "wecom"},
                            headers=manager_headers))
    assert body["sent"] >= 1
    assert body["recipients"][0]["channel"] == "wecom"
    assert body["recipients"][0]["delivered_via"] == "internal"
    assert body["context"]["ready_channels"] == ["internal"]


# --------------------------------------------------------------------------- #
# 通知中心：已读闭环 / 隔离
# --------------------------------------------------------------------------- #
def _push_a_report(client, manager_headers, title: str) -> int:
    """生成一份报告并推给自己，确保 manager 收件箱里至少有一条通知。

    不依赖别的用例先跑（否则就是「按文件字母序才通过」的假绿）。
    """
    created = data(client.post("/api/v1/reports/generate",
                               json={"report_type": "daily_digest", "title": title},
                               headers=manager_headers))
    pushed = data(client.post(f"/api/v1/reports/{created['id']}/push",
                              json={"roles": ["manager"]}, headers=manager_headers))
    assert pushed["sent"] >= 1
    return created["id"]


def test_notification_read_flow(client, manager_headers):
    _push_a_report(client, manager_headers, "已读闭环验证日报")

    inbox = data(client.get("/api/v1/notifications",
                            params={"unread_only": True}, headers=manager_headers))
    assert inbox["total"] >= 1
    target = inbox["items"][0]["id"]
    before = data(client.get("/api/v1/notifications/unread-count",
                             headers=manager_headers))["unread"]

    marked = data(client.post(f"/api/v1/notifications/{target}/read", headers=manager_headers))
    assert marked["read"] is True
    after = data(client.get("/api/v1/notifications/unread-count",
                            headers=manager_headers))["unread"]
    assert after == before - 1

    # 重复标记不刷新（幂等）
    again = data(client.post(f"/api/v1/notifications/{target}/read", headers=manager_headers))
    assert again["read"] is True
    assert data(client.get("/api/v1/notifications/unread-count",
                           headers=manager_headers))["unread"] == after


def test_read_all_marks_everything(client, manager_headers):
    _push_a_report(client, manager_headers, "全部已读验证日报")

    body = data(client.post("/api/v1/notifications/read-all", headers=manager_headers))
    assert body["marked"] >= 1
    assert data(client.get("/api/v1/notifications/unread-count",
                           headers=manager_headers))["unread"] == 0
    # 只剩未读的过滤条件应查不到东西
    unread_only = data(client.get("/api/v1/notifications",
                                  params={"unread_only": True}, headers=manager_headers))
    assert unread_only["total"] == 0


def test_notifications_are_isolated_between_users(client, manager_headers, advisor_headers):
    _push_a_report(client, manager_headers, "隔离验证日报")
    mgr = data(client.get("/api/v1/notifications", headers=manager_headers))
    assert mgr["total"] >= 1
    notif_id = mgr["items"][0]["id"]

    # advisor 看不到 manager 的通知（按收件人过滤）
    adv = data(client.get("/api/v1/notifications", headers=advisor_headers))
    assert all(item["id"] != notif_id for item in adv["items"])

    # 更不能标记别人的通知 —— 按「不存在」处理，不回显归属
    resp = client.post(f"/api/v1/notifications/{notif_id}/read", headers=advisor_headers)
    assert resp.status_code == 404


def test_visitor_has_no_inbox(client, visitor_headers):
    resp = client.get("/api/v1/notifications", headers=visitor_headers)
    assert resp.status_code == 403          # 访客账号不落库，显式拒绝而不是返回空列表


def test_notifications_require_auth(client):
    assert client.get("/api/v1/notifications").status_code in (401, 403)
