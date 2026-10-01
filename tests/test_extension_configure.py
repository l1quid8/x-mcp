"""An account connector must target only its self-host's configured origin."""

import importlib.util
import json
from pathlib import Path

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "browser-extension"
spec = importlib.util.spec_from_file_location("configure_extension", SOURCE / "configure.py")
extension = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extension)


def test_generated_extension_is_pinned_to_operator_origin(tmp_path):
    destination = extension.configure("https://mcp.example.com", tmp_path / "extension", "/my-mcp")
    manifest = json.loads((destination / "manifest.json").read_text())
    protocol = (destination / "protocol.mjs").read_text()
    assert manifest["host_permissions"] == ["https://mcp.example.com/*"]
    assert "connect-src https://mcp.example.com;" in manifest["content_security_policy"]["extension_pages"]
    assert "https://mcp.example.com/my-mcp/session-import" not in protocol  # constructed at runtime
    assert "export const ORIGIN = 'https://mcp.example.com'" in protocol
    assert "const IMPORT = `${ORIGIN}/my-mcp/session-import`" in protocol
    assert "mcp.example.invalid" not in protocol
    assert "mcp.example.invalid" in (SOURCE / "protocol.mjs").read_text()


@pytest.mark.parametrize("origin", ["", "http://mcp.example.com", "https://mcp.example.com/path",
                                     "https://user:pass@mcp.example.com", "https://mcp.example.invalid"])
def test_generator_rejects_unsafe_or_unconfigured_destination(tmp_path, origin):
    with pytest.raises(ValueError):
        extension.configure(origin, tmp_path / "extension")
    assert not (tmp_path / "extension").exists()
