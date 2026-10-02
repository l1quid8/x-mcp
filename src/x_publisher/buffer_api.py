"""Small, credential-safe client for Buffer's documented GraphQL API.

The adapter deliberately returns only selected post/channel fields. In particular,
upstream response bodies, exception strings, and the bearer key never leave it.
"""

from __future__ import annotations

from datetime import datetime
from ipaddress import ip_address
from urllib.parse import urlsplit

import httpx


BUFFER_ENDPOINT = "https://api.buffer.com"
_POST_FIELDS = "id channelId text status dueAt sentAt"
_ORGANIZATIONS = "query BufferOrganizations { account { organizations { id } } }"
_CHANNELS = """query BufferChannels($input: ChannelsInput!) {
  channels(input: $input) {
    id name displayName service isDisconnected isLocked isQueuePaused
  }
}"""
_GET_POST = f"""query BufferPost($input: PostInput!) {{
  post(input: $input) {{ {_POST_FIELDS} }}
}}"""
_CREATE_POST = f"""mutation BufferCreatePost($input: CreatePostInput!) {{
  createPost(input: $input) {{
    __typename
    ... on PostActionSuccess {{ post {{ {_POST_FIELDS} }} }}
    ... on MutationError {{ message }}
  }}
}}"""

_REJECTION_CODES = {
    "UNAUTHORIZED": ("unauthorized", "Buffer rejected the API key or session."),
    "FORBIDDEN": ("forbidden", "Buffer does not allow this action."),
    "NOT_FOUND": ("not_found", "Buffer could not find the requested item."),
    "RATE_LIMIT_EXCEEDED": ("rate_limited", "Buffer's API rate limit was reached."),
}
_TYPED_REJECTIONS = {
    "InvalidInputError": ("invalid_input", "Buffer rejected the post details."),
    "LimitReachedError": ("limit_reached", "The Buffer posting limit was reached."),
    "NotFoundError": ("not_found", "Buffer could not find the channel."),
    "UnauthorizedError": ("unauthorized", "Buffer does not allow this action."),
}
_POST_STATUSES = {"draft", "error", "needs_approval", "scheduled", "sending", "sent"}


class BufferError(Exception):
    """A safe error; ``definite`` means Buffer definitely rejected the write."""

    def __init__(self, code: str, message: str, definite: bool = True):
        super().__init__(message)
        self.code = code
        self.message = message
        self.definite = definite


class BufferAPI:
    def __init__(self, key: str, client_factory=httpx.AsyncClient):
        if not isinstance(key, str) or not key.strip():
            raise BufferError("missing_key", "A Buffer API key is required.")
        self._key = key.strip()
        self._client = client_factory(
            timeout=httpx.Timeout(20.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, query: str, variables: dict | None = None) -> dict:
        try:
            response = await self._client.post(
                BUFFER_ENDPOINT,
                headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
                json={"query": query, "variables": variables or {}},
            )
        except httpx.HTTPError:
            # Once a write is sent, a timeout/connection drop cannot prove whether
            # Buffer accepted it. Never retry an uncertain create automatically.
            raise BufferError("transport_error", "Could not confirm Buffer's response.", definite=False) from None

        if response.status_code != 200:
            if response.status_code == 401:
                raise BufferError("unauthorized", "Buffer rejected the API key or session.")
            if response.status_code == 403:
                raise BufferError("forbidden", "Buffer does not allow this action.")
            if response.status_code == 429:
                raise BufferError("rate_limited", "Buffer's API rate limit was reached.")
            if response.status_code >= 500:
                raise BufferError("server_error", "Buffer did not confirm the request.", definite=False)
            raise BufferError("api_rejected", "Buffer rejected the request.")

        try:
            payload = response.json()
        except ValueError:
            raise BufferError("invalid_response", "Buffer returned an unreadable response.", definite=False) from None
        if not isinstance(payload, dict):
            raise BufferError("invalid_response", "Buffer returned an unexpected response.", definite=False)

        errors = payload.get("errors")
        if errors:
            code = None
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                extension = errors[0].get("extensions")
                if isinstance(extension, dict):
                    code = extension.get("code")
            # A partial GraphQL response could contain an accepted mutation as
            # well as an error. Only a data-less known rejection is definite.
            no_data = payload.get("data") is None
            if no_data and isinstance(code, str) and code in _REJECTION_CODES:
                safe_code, message = _REJECTION_CODES[code]
                raise BufferError(safe_code, message)
            raise BufferError("api_error", "Buffer did not confirm the request.", definite=False)

        data = payload.get("data")
        if not isinstance(data, dict):
            raise BufferError("invalid_response", "Buffer returned an unexpected response.", definite=False)
        return data

    def _safe_string(self, value: object) -> str | None:
        if not isinstance(value, str):
            return None
        return value.replace(self._key, "[redacted]") if self._key in value else value

    def _post(self, raw: object) -> dict:
        if not isinstance(raw, dict):
            raise BufferError("invalid_response", "Buffer returned an unexpected post.", definite=False)
        post_id = self._safe_string(raw.get("id"))
        channel_id = self._safe_string(raw.get("channelId"))
        status = raw.get("status")
        if not post_id or not channel_id or status not in _POST_STATUSES:
            raise BufferError("invalid_response", "Buffer returned an incomplete post.", definite=False)
        return {
            "id": post_id,
            "channelId": channel_id,
            "text": self._safe_string(raw.get("text")),
            "status": status,
            "dueAt": self._safe_string(raw.get("dueAt")),
            "sentAt": self._safe_string(raw.get("sentAt")),
        }

    async def list_channels(self) -> list[dict]:
        account = (await self._request(_ORGANIZATIONS)).get("account")
        if not isinstance(account, dict) or not isinstance(account.get("organizations"), list):
            raise BufferError("invalid_response", "Buffer returned an unexpected account.", definite=False)
        channels: list[dict] = []
        for organization in account["organizations"]:
            if not isinstance(organization, dict) or not isinstance(organization.get("id"), str):
                raise BufferError("invalid_response", "Buffer returned an unexpected organization.", definite=False)
            organization_id = organization["id"]
            data = await self._request(_CHANNELS, {"input": {"organizationId": organization_id}})
            raw_channels = data.get("channels")
            if not isinstance(raw_channels, list):
                raise BufferError("invalid_response", "Buffer returned an unexpected channel list.", definite=False)
            for raw in raw_channels:
                if not isinstance(raw, dict) or raw.get("service") != "twitter":
                    continue
                channel_id = self._safe_string(raw.get("id"))
                username = self._safe_string(raw.get("name"))
                if not channel_id or not username:
                    raise BufferError("invalid_response", "Buffer returned an incomplete X channel.", definite=False)
                if any(not isinstance(raw.get(field), bool) for field in (
                    "isDisconnected", "isLocked", "isQueuePaused"
                )):
                    raise BufferError("invalid_response", "Buffer returned incomplete X channel availability.", definite=False)
                display_name = self._safe_string(raw.get("displayName"))
                channels.append({
                    "id": channel_id,
                    "name": display_name or username,
                    "username": username,
                    "service": "twitter",
                    "organizationId": self._safe_string(organization_id),
                    "isDisconnected": raw["isDisconnected"],
                    "isLocked": raw["isLocked"],
                    "isQueuePaused": raw["isQueuePaused"],
                })
        return channels

    async def get_post(self, post_id: str) -> dict:
        if not isinstance(post_id, str) or not post_id.strip():
            raise BufferError("invalid_input", "A Buffer post ID is required.")
        raw = (await self._request(_GET_POST, {"input": {"id": post_id}})).get("post")
        if raw is None:
            raise BufferError("not_found", "Buffer could not find the post.")
        return self._post(raw)

    async def create_post(
        self,
        channel_id: str,
        text: str,
        image_urls: list[str] | None = None,
        mode: str = "shareNow",
        due_at: str | None = None,
    ) -> dict:
        if not isinstance(channel_id, str) or not channel_id.strip():
            raise BufferError("invalid_input", "Select a Buffer X channel.")
        if not isinstance(text, str):
            raise BufferError("invalid_input", "Post text must be a string.")
        if image_urls is not None:
            if not isinstance(image_urls, list) or len(image_urls) > 4 or any(
                not isinstance(url, str) or not _public_https_url(url) for url in image_urls
            ):
                raise BufferError("invalid_input", "Use up to four public HTTPS image URLs.")
        image_urls = image_urls or []
        if not text.strip() and not image_urls:
            raise BufferError("invalid_input", "Add text or an image before posting.")
        if mode not in {"shareNow", "addToQueue", "customScheduled"}:
            raise BufferError("invalid_input", "Choose a supported Buffer posting mode.")
        if mode == "customScheduled":
            if not isinstance(due_at, str) or not _utc_datetime(due_at):
                raise BufferError("invalid_input", "Provide a UTC time for the scheduled post.")
        elif due_at is not None:
            raise BufferError("invalid_input", "A scheduled time requires customScheduled mode.")

        input_data: dict = {
            "channelId": channel_id,
            "text": text,
            "schedulingType": "automatic",
            "mode": mode,
            "assets": [{"image": {"url": url}} for url in image_urls],
        }
        if due_at is not None:
            input_data["dueAt"] = due_at
        result = (await self._request(_CREATE_POST, {"input": input_data})).get("createPost")
        if not isinstance(result, dict):
            raise BufferError("invalid_response", "Buffer did not confirm post creation.", definite=False)
        if "post" in result:
            return self._post(result["post"])
        typename = result.get("__typename")
        if typename in _TYPED_REJECTIONS:
            code, message = _TYPED_REJECTIONS[typename]
            raise BufferError(code, message)
        # UnexpectedError and RestProxyError can follow a write whose outcome is
        # unknown. Don't repeat it without checking Buffer first.
        raise BufferError("api_error", "Buffer did not confirm post creation.", definite=False)


def _public_https_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    hostname = parsed.hostname
    if parsed.scheme != "https" or not hostname or parsed.username or parsed.password:
        return False
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        address = ip_address(hostname)
    except ValueError:
        return True  # DNS reachability is checked by Buffer when the asset is used.
    return address.is_global


def _utc_datetime(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0
