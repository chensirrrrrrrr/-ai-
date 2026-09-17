"""统一通知的**真实外发路径**与失败兜底。

`test_notify.py` 覆盖的是「没配凭证 → 降级为站内」这一半；本文件补上另一半：
配好凭证之后，`_post_json` / `_deliver` / `send` 的**成功 / HTTP 失败 / 抛异常**三条分支。

为什么值得单独测：外发通道是「尽力而发」，最容易出的错不是发不出去，
而是**发失败了却当成发出去了** —— `send()` 的兜底逻辑（外发失败 → 落站内 +
把失败原因写进 `detail`）必须被锁死，否则「已触达」就成了一句无法验证的话。

全部走 monkeypatch，不联网、不依赖任何真实凭证。
"""
from __future__ import annotations

import pytest

from app.db import SessionLocal
from app.services import notify


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# _post_json：成功 / HTTP 非 2xx / 抛异常
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status: int, text: str = "") -> None:
        self.status_code = status
        self.text = text


class _FakeClient:
    """替掉 httpx.Client：只关心 `with ... as c: c.post(...)` 这个用法。"""

    def __init__(self, resp: _Resp) -> None:
        self._resp = resp

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def post(self, url: str, json: dict | None = None) -> _Resp:
        _FakeClient.last_request = {"url": url, "json": json}
        return self._resp


def _patch_httpx(monkeypatch, *, resp: _Resp | None = None, boom: Exception | None = None) -> None:
    def factory(*_args: object, **_kwargs: object) -> _FakeClient:
        if boom is not None:
            raise boom
        assert resp is not None
        return _FakeClient(resp)

    monkeypatch.setattr(notify.httpx, "Client", factory)


def test_post_json_success(monkeypatch):
    _patch_httpx(monkeypatch, resp=_Resp(200))
    ok, info = notify._post_json("http://127.0.0.1:9/hook", {"a": 1})
    assert ok is True
    assert info == "HTTP 200"
    assert _FakeClient.last_request["json"] == {"a": 1}


def test_post_json_reports_http_failure_with_body(monkeypatch):
    _patch_httpx(monkeypatch, resp=_Resp(500, "upstream boom"))
    ok, info = notify._post_json("http://127.0.0.1:9/hook", {})
    assert ok is False
    assert "HTTP 500" in info and "upstream boom" in info


def test_post_json_swallows_exception(monkeypatch):
    """DNS / 连接失败都不能抛出去 —— 通知是尽力而发，不许把主流程带崩。"""
    _patch_httpx(monkeypatch, boom=RuntimeError("connection refused"))
    ok, info = notify._post_json("http://127.0.0.1:9/hook", {})
    assert ok is False
    assert "RuntimeError" in info and "connection refused" in info


# --------------------------------------------------------------------------- #
# _deliver：各通道的载荷形状
# --------------------------------------------------------------------------- #
def test_deliver_wecom_uses_text_payload(monkeypatch):
    monkeypatch.setattr(notify.settings, "notify_wecom_webhook", "http://127.0.0.1:9/wecom", raising=False)
    seen: dict = {}

    def fake_post(url, payload):
        seen["url"] = url
        seen["payload"] = payload
        return True, "HTTP 200"

    monkeypatch.setattr(notify, "_post_json", fake_post)

    ok, detail = notify._deliver("wecom", "标题", None)
    assert ok is True
    assert seen["url"] == "http://127.0.0.1:9/wecom"
    assert seen["payload"]["msgtype"] == "text"
    assert seen["payload"]["text"]["content"] == "标题"      # 无正文时不拼换行
    assert detail == {"target": "wecom_webhook", "info": "HTTP 200"}


def test_deliver_webhook_carries_title_body_and_ts(monkeypatch):
    monkeypatch.setattr(notify.settings, "notify_webhook_url", "http://127.0.0.1:9/hook", raising=False)
    seen: dict = {}

    def fake_post(url, payload):
        seen.update(payload)
        return True, "HTTP 200"

    monkeypatch.setattr(notify, "_post_json", fake_post)

    ok, detail = notify._deliver("webhook", "标题", "正文")
    assert ok is True
    assert seen["title"] == "标题" and seen["body"] == "正文"
    assert seen["ts"]
    assert detail["target"] == "webhook"


def test_deliver_sms_is_honestly_not_wired():
    """短信没有适配器，必须如实返回失败原因，而不是假装成功。"""
    ok, detail = notify._deliver("sms", "标题", "正文")
    assert ok is False
    assert detail["target"] == "sms"
    assert "未接入" in detail["info"]


def test_deliver_internal_needs_no_network():
    ok, detail = notify._deliver("internal", "标题", "正文")
    assert ok is True
    assert detail == {"target": "internal"}


# --------------------------------------------------------------------------- #
# send：配好凭证之后的两条分支
# --------------------------------------------------------------------------- #
def _make_webhook_ready(monkeypatch, *, deliver_ok: bool) -> None:
    monkeypatch.setattr(notify, "channel_ready", lambda c: c in ("internal", "webhook"))
    monkeypatch.setattr(notify.settings, "notify_webhook_url", "http://127.0.0.1:9/hook", raising=False)
    monkeypatch.setattr(
        notify, "_post_json",
        lambda url, payload: (deliver_ok, "HTTP 200" if deliver_ok else "HTTP 500 rejected"),
    )


def test_send_uses_external_channel_when_ready(monkeypatch, db):
    _make_webhook_ready(monkeypatch, deliver_ok=True)
    row = notify.send(db, recipient_type="employee", recipient_id=9999,
                      category="system", title="外发成功", channel="webhook")
    assert row.channel == "webhook"
    assert row.delivered_via == "webhook"              # 没有降级
    assert (row.detail or {})["attempt"]["target"] == "webhook"
    assert not (row.detail or {}).get("degraded")
    assert row.status == "SENT"
    db.rollback()


def test_send_falls_back_to_internal_when_delivery_fails(monkeypatch, db):
    """外发失败 → 兜底落站内，但请求通道与失败原因都必须留着。"""
    _make_webhook_ready(monkeypatch, deliver_ok=False)
    row = notify.send(db, recipient_type="employee", recipient_id=9999,
                      category="system", title="外发失败", channel="webhook")
    assert row.channel == "webhook"                    # 请求的通道
    assert row.delivered_via == "internal"             # 实际生效的通道
    detail = row.detail or {}
    assert detail.get("degraded") is True
    assert "外发失败" in detail.get("reason", "")
    assert detail["attempt"]["info"].startswith("HTTP 500")
    db.rollback()


def test_send_truncates_overlong_title(monkeypatch, db):
    monkeypatch.setattr(notify.settings, "notify_title_max", 8, raising=False)
    row = notify.send(db, recipient_type="employee", recipient_id=9999,
                      category="system", title="标题" * 20)
    assert len(row.title) == 8
    db.rollback()


def test_send_blank_title_falls_back(monkeypatch, db):
    row = notify.send(db, recipient_type="employee", recipient_id=9999,
                      category="system", title="   ")
    assert row.title == "系统通知"
    db.rollback()


# --------------------------------------------------------------------------- #
# mark_read 反向 / 群发 / 通道总览
# --------------------------------------------------------------------------- #
def test_mark_read_can_be_reverted(db):
    row = notify.send(db, recipient_type="employee", recipient_id=9999,
                      category="system", title="标回未读")
    assert notify.mark_read(db, row.id, recipient_type="employee",
                            recipient_id=9999).read_at is not None
    reverted = notify.mark_read(db, row.id, recipient_type="employee",
                                recipient_id=9999, read=False)
    assert reverted.read_at is None
    db.rollback()


def test_send_many_broadcasts_same_content(db):
    rows = notify.send_many(
        db,
        [{"recipient_type": "employee", "recipient_id": 9998},
         {"recipient_type": "employee", "recipient_id": 9997}],
        category="system", title="群发",
    )
    assert len(rows) == 2
    assert {r.title for r in rows} == {"群发"}
    db.rollback()


def test_channel_status_lists_every_channel():
    status = notify.channel_status()
    assert status["default"] == notify.settings.notify_default_channel
    by_name = {c["name"]: c for c in status["channels"]}
    assert set(by_name) == {"internal", "wecom", "sms", "webhook"}
    assert by_name["internal"]["ready"] is True and by_name["internal"]["note"] == ""
    assert set(status["ready"]) <= set(by_name)
    assert "internal" in status["ready"]


def test_dump_exposes_delivery_and_read_state(db):
    row = notify.send(db, recipient_type="employee", recipient_id=9999,
                      category="system", title="字段验证")
    payload = notify.dump(row)
    assert payload["category_label"] == notify.CATEGORY_LABELS["system"]
    assert payload["degraded"] is False
    assert payload["read_at"] is None
    assert payload["delivered_via"] == "internal"
    db.rollback()
