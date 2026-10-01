"""A cloned server must not acquire anyone else's MCP endpoint by default."""

import os
import subprocess
import sys

from x_publisher.core import ORIGIN, RESOURCE


def test_test_origin_is_isolated():
    assert ORIGIN == "https://mcp.example.test"
    assert RESOURCE == "https://mcp.example.test/x-mcp/mcp"


def test_origin_is_required_before_import():
    environment = os.environ.copy()
    environment.pop("X_MCP_ORIGIN", None)
    result = subprocess.run([sys.executable, "-c", "import x_publisher.core"],
                            env=environment, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "Set X_MCP_ORIGIN" in result.stderr
