# Source provenance and attribution

X MCP combines an account-scoped publishing and cleanup implementation with
reader code adapted from [Alastrantia/nitter-mcp](https://github.com/Alastrantia/nitter-mcp).
It is one MCP server, with one transport and authorization layer. The former
reader and publisher deployments are not included or operated by this repo.

The adapted **Nitter MCP reader code** is MIT licensed, copyright © 2026
Alastrantia. Its [license notice](../third_party/nitter-mcp/LICENSE) is retained
in the repository and included in the Python wheel. The reader currently
contacts public Nitter RSS instances. This repository does not include or host
the separate [Nitter mirror software](https://github.com/zedeus/nitter).

The publishing and session-cleanup backend uses
[Twikit](https://github.com/d60/twikit) as a pinned Git dependency rather than
copied source. Twikit is [MIT licensed](https://github.com/d60/twikit/blob/main/LICENSE),
and its exact revision is recorded in `pyproject.toml` and `uv.lock`.

The original publisher implementation came from a private project. Its license
will be stated separately if the owner chooses to grant reuse rights. The
server has no default MCP origin or hosted endpoint; every self-hosting
installation must set `X_MCP_ORIGIN` to its own HTTPS origin. This repository
contains no X account sessions or server credentials.
