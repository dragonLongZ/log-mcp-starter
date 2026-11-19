"""自定义白名单相关 HTTP 接口测试（/dashboard/allowlist*）。

用 FastAPI TestClient 直接打网关应用；白名单文件隔离到临时路径，不碰 config/。
"""
from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import system_allowlist
from system_guard import Verdict, validate_system_info_command


@pytest.fixture()
def allowlist_file():
    path = Path(tempfile.gettempdir()) / f"log_mcp_api_{uuid.uuid4().hex}.yaml"
    system_allowlist.set_path(path)
    yield path
    system_allowlist.set_path(None)
    path.unlink(missing_ok=True)


@pytest.fixture()
def client(allowlist_file):
    import gateway_host

    with TestClient(gateway_host.app) as test_client:
        yield test_client


@pytest.fixture()
def auth():
    import gateway_host

    return {"Authorization": f"Bearer {gateway_host.read_token()}"}


class TestAllowlistApi:
    def test_requires_bearer(self, client):
        assert client.get("/dashboard/allowlist").status_code == 401

    def test_list_empty(self, client, auth):
        body = client.get("/dashboard/allowlist", headers=auth).json()
        assert body["entries"] == []
        assert body["path"]

    def test_add_then_check_then_delete(self, client, auth):
        added = client.post(
            "/dashboard/allowlist", headers=auth, json={"name": "htop", "note": "进程"}
        )
        assert added.status_code == 200
        assert added.json()["entries"][0]["name"] == "htop"

        verdict = client.post(
            "/dashboard/allowlist/check", headers=auth, json={"command": "htop"}
        ).json()
        assert verdict["verdict"] == "allowed"

        deleted = client.delete("/dashboard/allowlist", headers=auth, params={"name": "htop"})
        assert deleted.status_code == 200
        assert deleted.json()["entries"] == []

        after = client.post(
            "/dashboard/allowlist/check", headers=auth, json={"command": "htop"}
        ).json()
        assert after["verdict"] == "unknown"

    def test_rejects_shell_metacharacters(self, client, auth):
        response = client.post(
            "/dashboard/allowlist", headers=auth, json={"name": "ps; rm -rf /"}
        )
        assert response.status_code == 400
        assert "metacharacter" in response.json()["detail"]

    def test_rejects_blocked_command(self, client, auth):
        response = client.post("/dashboard/allowlist", headers=auth, json={"name": "sudo"})
        assert response.status_code == 400
        assert "unlock blocked" in response.json()["detail"]

    def test_delete_missing_entry_is_400(self, client, auth):
        response = client.delete("/dashboard/allowlist", headers=auth, params={"name": "htop"})
        assert response.status_code == 400

    def test_denied_flag_survives_roundtrip(self, client, auth):
        client.post(
            "/dashboard/allowlist",
            headers=auth,
            json={"name": "ncdu", "denied": ["-o"], "note": "目录占用"},
        )
        assert validate_system_info_command("ncdu").verdict == Verdict.ALLOWED
        assert validate_system_info_command("ncdu -o out.txt").verdict == Verdict.REJECTED

    def test_check_does_not_require_asset_connection(self, client, auth):
        # 试运行只做静态判定，不应该因为资产未连接而失败
        body = client.post(
            "/dashboard/allowlist/check", headers=auth, json={"command": "docker ps"}
        ).json()
        assert body["verdict"] == "allowed"

    def test_check_reports_rejection_reason(self, client, auth):
        body = client.post(
            "/dashboard/allowlist/check", headers=auth, json={"command": "ps aux > /tmp/x"}
        ).json()
        assert body["verdict"] == "rejected"
        assert "redirect" in body["message"].lower()


class TestDashboardPage:
    def test_dashboard_requires_valid_token(self, client):
        page = client.get("/dashboard", params={"token": "wrong"})
        assert "登录" in page.text

    def test_dashboard_contains_allowlist_panel(self, client):
        import gateway_host

        page = client.get("/dashboard", params={"token": gateway_host.read_token()})
        assert page.status_code == 200
        assert "自定义命令白名单" in page.text
        assert "/dashboard/allowlist" in page.text
        # token 通过 json 注入为 JS 字面量，不再做裸字符串替换
        assert f'var T="{gateway_host.read_token()}"' in page.text
        assert "__TOKEN__" not in page.text
