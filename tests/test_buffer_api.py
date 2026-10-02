"""Buffer API contract checks use only mocked HTTP responses."""

import json

import httpx
import pytest

from x_publisher.buffer_api import BufferAPI, BufferError


def _api(handler):
    return BufferAPI(
        "TEST_BUFFER_SECRET",
        lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs),
    )


@pytest.mark.asyncio
async def test_list_channels_queries_organizations_and_filters_x():
    requests = []

    def respond(request):
        assert str(request.url) == "https://api.buffer.com"
        assert request.headers["Authorization"] == "Bearer TEST_BUFFER_SECRET"
        body = json.loads(request.content)
        requests.append(body)
        if "BufferOrganizations" in body["query"]:
            return httpx.Response(200, json={"data": {"account": {
                "organizations": [{"id": "org_one"}, {"id": "org_two"}],
            }}})
        assert "BufferChannels" in body["query"]
        assert "serviceId" in body["query"]
        organization_id = body["variables"]["input"]["organizationId"]
        if organization_id == "org_one":
            channels = [
                {"id": "x_1", "name": "owner_handle", "displayName": "Owner", "service": "twitter",
                 "serviceId": "1234567890123456789",
                 "isDisconnected": False, "isLocked": False, "isQueuePaused": False},
                {"id": "b_1", "name": "blue_handle", "displayName": "Other", "service": "bluesky",
                 "isDisconnected": False, "isLocked": False, "isQueuePaused": False},
            ]
        else:
            channels = [
                {"id": "x_2", "name": "second_handle", "displayName": None, "service": "twitter",
                 "serviceId": "not-an-X-id",
                 "isDisconnected": True, "isLocked": False, "isQueuePaused": True},
            ]
        return httpx.Response(200, json={"data": {"channels": channels}})

    api = _api(respond)
    try:
        channels = await api.list_channels()
    finally:
        await api.close()

    assert len(requests) == 3
    assert channels == [
        {"id": "x_1", "name": "Owner", "username": "owner_handle", "service": "twitter",
         "organizationId": "org_one", "isDisconnected": False, "isLocked": False,
         "isQueuePaused": False, "x_account_id": "1234567890123456789"},
        {"id": "x_2", "name": "second_handle", "username": "second_handle", "service": "twitter",
         "organizationId": "org_two", "isDisconnected": True, "isLocked": False,
         "isQueuePaused": True},
    ]


@pytest.mark.parametrize("service_id", ["", "0", "00123", "-123", "+123", "１２３",
                                        "123.0", "18446744073709551616", None, 123])
@pytest.mark.asyncio
async def test_noncanonical_buffer_service_id_never_enables_direct_fallback(service_id):
    def respond(request):
        query = json.loads(request.content)["query"]
        if "BufferOrganizations" in query:
            return httpx.Response(200, json={"data": {"account": {
                "organizations": [{"id": "org_one"}],
            }}})
        return httpx.Response(200, json={"data": {"channels": [{
            "id": "x_1", "name": "handle", "displayName": "Account", "service": "twitter",
            "serviceId": service_id, "isDisconnected": False, "isLocked": False,
            "isQueuePaused": False,
        }]}})

    api = _api(respond)
    try:
        channels = await api.list_channels()
    finally:
        await api.close()
    assert len(channels) == 1
    assert "x_account_id" not in channels[0]


@pytest.mark.asyncio
async def test_create_image_post_uses_documented_assets_and_returns_receipt():
    def respond(request):
        body = json.loads(request.content)
        assert "mutation BufferCreatePost" in body["query"]
        assert "... on MutationError" in body["query"]
        assert body["variables"]["input"] == {
            "channelId": "x_1",
            "text": "An image",
            "schedulingType": "automatic",
            "mode": "shareNow",
            "assets": [{"image": {"url": "https://images.example.com/picture.jpg"}}],
        }
        return httpx.Response(200, json={"data": {"createPost": {
            "__typename": "PostActionSuccess",
            "post": {"id": "post_123", "channelId": "x_1", "text": "An image",
                     "status": "sending", "dueAt": None, "sentAt": None},
        }}})

    api = _api(respond)
    try:
        post = await api.create_post("x_1", "An image", ["https://images.example.com/picture.jpg"])
    finally:
        await api.close()

    assert post == {"id": "post_123", "channelId": "x_1", "text": "An image",
                    "status": "sending", "dueAt": None, "sentAt": None}


@pytest.mark.asyncio
async def test_typed_rejection_is_definite_and_never_returns_upstream_message():
    api = _api(lambda _request: httpx.Response(200, json={"data": {"createPost": {
        "__typename": "InvalidInputError", "message": "TEST_BUFFER_SECRET and private upstream details",
    }}}))
    try:
        with pytest.raises(BufferError) as caught:
            await api.create_post("x_1", "Text")
    finally:
        await api.close()

    assert caught.value.code == "invalid_input"
    assert caught.value.definite is True
    assert "TEST_BUFFER_SECRET" not in str(caught.value)
    assert "private upstream details" not in str(caught.value)


@pytest.mark.asyncio
async def test_transport_failure_after_create_has_uncertain_outcome():
    def respond(_request):
        raise httpx.ReadTimeout("TEST_BUFFER_SECRET and private upstream details")

    api = _api(respond)
    try:
        with pytest.raises(BufferError) as caught:
            await api.create_post("x_1", "Text")
    finally:
        await api.close()

    assert caught.value.code == "transport_error"
    assert caught.value.definite is False
    assert "TEST_BUFFER_SECRET" not in str(caught.value)
    assert "private upstream details" not in str(caught.value)
