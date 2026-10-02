"""Root admin commands must leave state writable by the documented service user."""
import sys
from types import SimpleNamespace
from unittest.mock import Mock

from x_publisher import admin
from test_publisher import store


def test_admin_accounts_preserves_default_service_ownership(store, tmp_path, monkeypatch):
    lookup = Mock(return_value=SimpleNamespace(pw_uid=123, pw_gid=456))
    chown = Mock()
    monkeypatch.setattr(admin.os, "geteuid", lambda: 0)
    monkeypatch.setattr(admin.pwd, "getpwnam", lookup)
    monkeypatch.setattr(admin.os, "chown", chown)
    monkeypatch.setattr(admin, "runtime_store", lambda: store)
    monkeypatch.setattr(sys, "argv", ["x-mcp-admin", "accounts"])
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path / "credentials"))
    monkeypatch.delenv("X_MCP_SERVICE_USER", raising=False)

    admin.main()

    lookup.assert_called_once_with("xmcp")
    assert any(call.args[0] == store.directory for call in chown.call_args_list)
