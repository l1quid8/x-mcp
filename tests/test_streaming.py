import io
from pathlib import Path
import tracemalloc
from types import SimpleNamespace

import av
import pytest

from x_publisher.backend import XBackend
from x_publisher.core import Problem
from x_publisher.media import inspect_media, PublicResolver, MediaStore
from test_publisher import store


async def test_large_video_streaming_memory(tmp_path):
    path = tmp_path / "large.mp4"
    with path.open("wb") as f:
        f.truncate(512 * 1024**2)
    sizes = []
    class V11:
        async def upload_media_init(self, mime, size, category, long):
            assert size == path.stat().st_size
            return {"media_id":"123"}, None
        async def upload_media_append(self, long, mid, index, chunk):
            assert index == len(sizes)
            sizes.append(len(chunk.getbuffer()))
        async def upload_media_finelize(self, long, mid):
            return {"processing_info":{"state":"succeeded"}}, None
    backend = object.__new__(XBackend)
    backend.client = SimpleNamespace(v11=V11())
    tracemalloc.start()
    assert await backend.upload(path,{"kind":"video","mime":"video/mp4"},True) == "123"
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert sum(sizes) == 512*1024**2
    assert max(sizes) <= 4*1024**2
    assert peak < 20*1024**2


async def test_processing_failure_is_not_success(tmp_path):
    path = tmp_path/"file.mp4"
    path.write_bytes(b"fake")
    class V11:
        async def upload_media_init(self,*args): return {"media_id":"1"},None
        async def upload_media_append(self,*args): pass
        async def upload_media_finelize(self,*args): return {"processing_info":{"state":"failed","error":{"code":1}}},None
    backend = object.__new__(XBackend)
    backend.client = SimpleNamespace(v11=V11())
    with pytest.raises(Problem,match="process"):
        await backend.upload(path,{"kind":"video","mime":"video/mp4"})


async def test_dns_resolution_blocks_private_ip():
    resolver = PublicResolver()
    class Resolver:
        async def resolve(self,*args): return [{"host":"10.0.0.1"}]
        async def close(self): pass
    await resolver.resolver.close()
    resolver.resolver = Resolver()
    with pytest.raises(Problem,match="private"):
        await resolver.resolve("public-looking.example",443)
    await resolver.close()


def test_probe_real_video_and_reject_playlist(tmp_path):
    path = tmp_path/"clip.mp4"
    with av.open(str(path),mode="w") as output:
        stream = output.add_stream("libx264", rate=24)
        stream.width, stream.height, stream.pix_fmt = 64,64,"yuv420p"
        for _ in range(24):
            frame = av.VideoFrame(64,64,"yuv420p")
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            for packet in stream.encode(frame): output.mux(packet)
        for packet in stream.encode(): output.mux(packet)
    metadata = inspect_media(path)
    assert metadata["kind"] == "video" and 0.9 <= metadata["duration"] <= 1.1
    fake = tmp_path/"playlist.mp4"
    fake.write_text("#EXTM3U\n#EXTINF:1\nhttp://127.0.0.1/private\n")
    with pytest.raises(Problem,match="unsupported"):
        inspect_media(fake)


async def test_expired_media_removed_and_not_reused(store):
    media = MediaStore(store)
    item = media.begin("1","expired",30)
    with store.db:
        store.db.execute("UPDATE media SET expires=0")
    with pytest.raises(Problem,match="expired"):
        store.media(item["media_id"],"1")
    media.cleanup()
    assert not (store.media_directory/item["media_id"]).exists()
