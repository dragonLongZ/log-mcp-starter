"""资产与白名单相关 HTTP 接口测试（/dashboard/targets*、/dashboard/allowlist*）。

用 FastAPI TestClient 直接打网关应用；数据库与 settings 由 conftest 隔离到临时目录。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import config_store


@pytest.fixture()
def client():
    import gateway_host

    with TestClient(gateway_host.app) as test_client:
        yield test_client


@pytest.fixture()
def auth():
    import gateway_host

    return {"Authorization": f"Bearer {gateway_host.read_token()}"}


def payload(**overrides):
    body = {
        "name": "srv-api",
        "description": "订单服务",
        "env": "测试",
        "kind": "linux",
        "ssh_username": "user#root#10.0.0.11",
        "modules": {"order-api": {"log_dir": "/data/apps/order-api/logs", "description": "订单"}},
    }
    body.update(overrides)
    return body


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
        from system_guard import Verdict, validate_system_info_command

        client.post(
            "/dashboard/allowlist",
            headers=auth,
            json={"name": "ncdu", "denied": ["-o"], "note": "目录占用"},
        )
        assert validate_system_info_command("ncdu").verdict == Verdict.ALLOWED
        assert validate_system_info_command("ncdu -o out.txt").verdict == Verdict.REJECTED

    def test_denied_subcommands_survive_roundtrip(self, client, auth):
        """jcmd 场景：禁用参数是子命令而不是 flag，网页提交后必须能保存并生效。"""
        from system_guard import Verdict, validate_system_info_command

        response = client.post(
            "/dashboard/allowlist",
            headers=auth,
            json={
                "name": "jcmd",
                "denied": ["VM.set_flag", "GC.run", "GC.run_finalization"],
                "note": "JVM 诊断",
            },
        )
        assert response.status_code == 200, response.json()
        assert response.json()["entries"][0]["denied"] == [
            "VM.set_flag", "GC.run", "GC.run_finalization",
        ]
        assert validate_system_info_command("jcmd 1234 VM.version").verdict == Verdict.ALLOWED
        assert validate_system_info_command("jcmd 1234 GC.run").verdict == Verdict.REJECTED

    def test_check_does_not_require_asset_connection(self, client, auth):
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


class TestTargetsApi:
    def test_requires_bearer(self, client):
        assert client.get("/dashboard/targets").status_code == 401

    def test_list_empty(self, client, auth):
        body = client.get("/dashboard/targets", headers=auth).json()
        assert body["targets"] == []
        assert body["db_path"].endswith(".db")
        assert body["writes_enabled"] is True

    def test_create_then_appears_in_targets(self, client, auth):
        """网页新增资产后，模型侧的 /targets 立刻能看到（免重启）。"""
        created = client.post("/dashboard/targets", headers=auth, json=payload())
        assert created.status_code == 200, created.json()

        listed = client.get("/targets", headers=auth).json()["targets"]
        assert "srv-api" in listed
        assert listed["srv-api"]["env"] == "测试"
        assert listed["srv-api"]["modules"]["order-api"]["log_dir"] == "/data/apps/order-api/logs"

    def test_new_target_starts_disconnected(self, client, auth):
        client.post("/dashboard/targets", headers=auth, json=payload())
        body = client.get("/dashboard/targets", headers=auth).json()
        assert body["targets"][0]["connection"]["state"] == "disconnected"

    def test_invalid_log_dir_is_400_and_writes_nothing(self, client, auth):
        response = client.post(
            "/dashboard/targets",
            headers=auth,
            json=payload(modules={"m": {"log_dir": "relative/path"}}),
        )
        assert response.status_code == 400
        assert client.get("/dashboard/targets", headers=auth).json()["targets"] == []

    def test_validate_endpoint_does_not_write(self, client, auth):
        body = client.post(
            "/dashboard/targets/validate", headers=auth, json=payload()
        ).json()
        assert body["valid"] is True
        assert client.get("/dashboard/targets", headers=auth).json()["targets"] == []

    def test_validate_reports_error(self, client, auth):
        body = client.post(
            "/dashboard/targets/validate", headers=auth, json=payload(ssh_username="")
        ).json()
        assert body["valid"] is False
        assert body["error"]

    def test_update_overwrites_modules(self, client, auth):
        client.post("/dashboard/targets", headers=auth, json=payload())
        client.post(
            "/dashboard/targets",
            headers=auth,
            json=payload(modules={"other": {"log_dir": "/data/other", "description": ""}}),
        )
        target = client.get("/dashboard/targets", headers=auth).json()["targets"][0]
        assert list(target["modules"]) == ["other"]

    def test_delete_removes_from_targets(self, client, auth):
        client.post("/dashboard/targets", headers=auth, json=payload())
        deleted = client.delete(
            "/dashboard/targets", headers=auth, params={"name": "srv-api"}
        )
        assert deleted.status_code == 200
        assert client.get("/targets", headers=auth).json()["targets"] == {}

    def test_delete_missing_is_400(self, client, auth):
        response = client.delete("/dashboard/targets", headers=auth, params={"name": "nope"})
        assert response.status_code == 400

    def test_disable_hides_target_but_keeps_row(self, client, auth):
        client.post("/dashboard/targets", headers=auth, json=payload())
        response = client.post(
            "/dashboard/targets/enabled", headers=auth,
            json={"name": "srv-api", "enabled": False},
        )
        assert response.status_code == 200
        assert client.get("/targets", headers=auth).json()["targets"] == {}
        rows = client.get("/dashboard/targets", headers=auth).json()["targets"]
        assert rows[0]["enabled"] is False

    def test_k8s_target_without_modules(self, client, auth):
        body = payload(
            name="k8s-a", kind="k8s", modules={},
            k8s_context="dev", k8s_namespaces=["default"],
        )
        response = client.post("/dashboard/targets", headers=auth, json=body)
        assert response.status_code == 200, response.json()
        listed = client.get("/targets", headers=auth).json()["targets"]["k8s-a"]
        assert listed["kind"] == "k8s"
        assert listed["k8s"] == {"context": "dev", "namespaces": ["default"]}

    def test_import_dry_run_then_apply(self, client, auth):
        content = """
targets:
  srv-imported:
    description: "导入的资产"
    ssh_username: "user#root#10.0.0.20"
    modules:
      m:
        log_dir: "/data/m"
"""
        dry = client.post(
            "/dashboard/targets/import", headers=auth,
            json={"content": content, "dry_run": True},
        ).json()
        assert dry["accepted"] == ["srv-imported"]
        assert client.get("/dashboard/targets", headers=auth).json()["targets"] == []

        applied = client.post(
            "/dashboard/targets/import", headers=auth,
            json={"content": content, "dry_run": False},
        ).json()
        assert applied["applied"] is True
        assert "srv-imported" in client.get("/targets", headers=auth).json()["targets"]

    def test_export_returns_yaml(self, client, auth):
        client.post("/dashboard/targets", headers=auth, json=payload())
        import gateway_host

        response = client.get(
            "/dashboard/targets/export",
            params={"token": gateway_host.read_token()},
        )
        assert response.status_code == 200
        assert "srv-api" in response.text
        assert "order-api" in response.text

    def test_export_requires_valid_token(self, client):
        assert client.get("/dashboard/targets/export", params={"token": "wrong"}).status_code == 401

    def test_write_disabled_returns_403(self, client, auth, write_disabled):
        response = client.post("/dashboard/targets", headers=auth, json=payload())
        assert response.status_code == 403
        assert "write_enabled" in response.json()["detail"]
        # 读接口不受影响
        assert client.get("/dashboard/targets", headers=auth).status_code == 200

    def test_write_disabled_also_blocks_allowlist_writes(self, client, auth, write_disabled):
        """白名单也是写入：只读分享 token 时不能让人顺手扩权。"""
        added = client.post("/dashboard/allowlist", headers=auth, json={"name": "htop"})
        assert added.status_code == 403
        assert "write_enabled" in added.json()["detail"]

        deleted = client.delete("/dashboard/allowlist", headers=auth, params={"name": "htop"})
        assert deleted.status_code == 403

        # 读与试运行不受影响
        assert client.get("/dashboard/allowlist", headers=auth).status_code == 200
        assert client.post(
            "/dashboard/allowlist/check", headers=auth, json={"command": "docker ps"}
        ).status_code == 200

    def test_allowlist_writes_work_when_enabled(self, client, auth):
        """开关打开时白名单写入照常可用（避免修过头）。"""
        assert client.post(
            "/dashboard/allowlist", headers=auth, json={"name": "htop"}
        ).status_code == 200
        assert client.delete(
            "/dashboard/allowlist", headers=auth, params={"name": "htop"}
        ).status_code == 200


class TestConfigWarnings:
    def test_broken_row_surfaces_as_warning(self, client, auth):
        """坏资产被跳过但网关照常服务，页面与 /health 都能看到告警。"""
        config_store.init_db()
        with config_store.connect() as conn:
            conn.execute(
                "INSERT INTO targets (name, description, env, kind, ssh_username, "
                "k8s_context, k8s_namespaces, enabled, created_at, updated_at) "
                "VALUES ('broken', '', '', 'linux', '', '', '[]', 1, '', '')"
            )
        health = client.get("/health", headers=auth).json()
        assert any("broken" in warning for warning in health["config_warnings"])
        assert health["assets_total"] == 0


class TestDashboardPage:
    def test_dashboard_requires_valid_token(self, client):
        page = client.get("/dashboard", params={"token": "wrong"})
        assert "登录" in page.text

    def test_lookup_takes_single_argument(self, client):
        """回归：lookup(o,s) 双参数签名会让状态列渲染成空（曾导致网页看不到「已连接」）。"""
        import re

        import gateway_host

        page = client.get("/dashboard", params={"token": gateway_host.read_token()})
        js = re.search(r"<script>(.*?)</script>", page.text, re.S).group(1)
        assert re.search(r"function lookup\(\s*s\s*\)", js), "lookup 必须只接收一个状态参数"
        assert "lookup(c.state)" in js

    def test_dashboard_contains_allowlist_panel(self, client):
        import gateway_host

        page = client.get("/dashboard", params={"token": gateway_host.read_token()})
        assert page.status_code == 200
        assert "自定义命令白名单" in page.text
        assert "/dashboard/allowlist" in page.text
        # token 通过 json 注入为 JS 字面量，不再做裸字符串替换
        assert f'var T="{gateway_host.read_token()}"' in page.text
        assert "__TOKEN__" not in page.text

    def test_dashboard_contains_targets_panel(self, client):
        import gateway_host

        page = client.get("/dashboard", params={"token": gateway_host.read_token()})
        assert "资产配置" in page.text
        assert "/dashboard/targets" in page.text
