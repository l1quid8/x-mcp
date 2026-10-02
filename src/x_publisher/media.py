"""Bounded, private staging and resumable transfers; never uploads to X."""
import asyncio
import hashlib
import ipaddress
import json
import os
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp
import av
from PIL import Image

from .core import Problem, canonical, identifier

CHUNK = 1024 * 1024
MAX_FILE = 8 * 1024**3
Image.MAX_IMAGE_PIXELS = 40_000_000


def public_url(url):
    p = urlsplit(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise Problem("invalid_media_url", "Media must use a public HTTPS URL on port 443")
    if p.hostname.lower() == "localhost" or p.hostname.lower().endswith((".local", ".localhost")):
        raise Problem("invalid_media_url", "Private destinations are not allowed")
    try:
        address = ipaddress.ip_address(p.hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise Problem("invalid_media_url", "Private destinations are not allowed")
    return url


class PublicResolver(aiohttp.abc.AbstractResolver):
    def __init__(self):
        self.resolver = aiohttp.resolver.ThreadedResolver()

    async def resolve(self, host, port=0, family=0):
        records = await self.resolver.resolve(host, port, family)
        if not records or any(not ipaddress.ip_address(r["host"]).is_global for r in records):
            raise Problem("invalid_media_url", "Media DNS resolved to a private destination")
        return records

    async def close(self):
        await self.resolver.close()


def inspect_media(path: Path):
    try:
        with Image.open(path) as image:
            fmt = image.format
            width, height = image.size
            image.verify()
        if fmt not in {"JPEG", "PNG", "GIF", "WEBP"}:
            raise Problem("unsupported_media", "Use JPEG, PNG, WEBP, GIF, MP4 or MOV media")
        return {"kind": "gif" if fmt == "GIF" else "image", "mime": Image.MIME[fmt],
                "width": width, "height": height}
    except Problem:
        raise
    except Exception:
        pass
    try:
        with av.open(str(path), format="mov", options={"protocol_whitelist": "file", "probesize": "1048576", "analyzeduration": "5000000"}) as container:
            if not ({"mov", "mp4"} & set(container.format.name.split(","))):
                raise ValueError("Unsupported container")
            videos = list(container.streams.video)
            if len(videos) != 1 or videos[0].codec_context.name != "h264":
                raise ValueError("Use H.264 video")
            if any(s.codec_context.name != "aac" for s in container.streams.audio):
                raise ValueError("Use AAC audio")
            duration = container.duration / av.time_base if container.duration else None
            if not duration or duration <= 0:
                raise ValueError("Unknown video duration")
            v = videos[0]
            return {"kind": "video", "mime": "video/mp4", "duration": duration,
                    "width": v.width, "height": v.height}
    except Exception as exc:
        raise Problem("unsupported_media", "Media is invalid or unsupported; use images, GIF, or H.264/AAC MP4/MOV") from exc


class MediaStore:
    def __init__(self, store, quota=24 * 1024**3):
        self.store, self.quota = store, quota
        self.locks = {}
        self.transfers = asyncio.Semaphore(2)

    def begin(self, account, name, size, sha256=None):
        self.store.account(account)
        if not isinstance(size, int) or not 0 < size <= MAX_FILE:
            raise Problem("invalid_size", "Declared size must be between 1 byte and 8 GiB")
        if sha256 and (len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256)):
            raise Problem("invalid_digest", "Expected a lowercase SHA-256 digest")
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            reserved = self.store.db.execute("SELECT COALESCE(SUM(size),0) FROM media").fetchone()[0]
            if reserved + size > self.quota:
                raise Problem("storage_quota", "Private media staging quota exceeded")
            mid = identifier()
            self.store.db.execute("INSERT INTO media(id,account,name,size,sha256,status,expires) VALUES (?,?,?,?,?,'uploading',?)", (
                mid, account, Path(name).name[:200], size, sha256, time.time() + 86400))
        (self.store.media_directory / mid).touch(mode=0o600)
        return {"media_id": mid, "size": size, "offset": 0}

    async def append(self, mid, account, offset, stream):
        lock = self.locks.setdefault(mid, asyncio.Lock())
        if lock.locked():
            raise Problem("upload_busy", "Another transfer is writing this attachment")
        async with self.transfers, lock:
            row = self.store.media(mid, account)
            if row["status"] != "uploading" or row["importing"]:
                raise Problem("upload_state", "This attachment cannot accept chunks")
            if offset != row["offset"]:
                raise Problem("offset_mismatch", f"Resume at offset {row['offset']}")
            path = self.store.media_directory / mid
            written = 0
            try:
                with path.open("r+b") as f:
                    f.seek(offset)
                    f.truncate()
                    async for chunk in stream:
                        if written + len(chunk) > 8 * CHUNK or offset + written + len(chunk) > row["size"]:
                            raise Problem("chunk_too_large", "Use chunks up to 8 MiB within the declared file size")
                        f.write(chunk)
                        written += len(chunk)
                    f.flush()
                    os.fsync(f.fileno())
                with self.store.db:
                    self.store.db.execute("UPDATE media SET offset=? WHERE id=?", (offset + written, mid))
            except BaseException:
                with path.open("r+b") as f:
                    f.truncate(offset)
                raise
            return {"media_id": mid, "offset": offset + written, "size": row["size"]}

    async def finish(self, mid, account):
        lock = self.locks.setdefault(mid, asyncio.Lock())
        async with lock:
            row = self.store.media(mid, account)
            if row["status"] == "ready":
                return {"media_id": mid, **json.loads(row["metadata"])}
            if row["offset"] != row["size"]:
                raise Problem("incomplete_upload", "Upload all bytes before finishing")
            path = self.store.media_directory / mid

            def validate():
                with path.open("rb") as f:
                    digest = hashlib.file_digest(f, "sha256").hexdigest()
                if row["sha256"] and digest != row["sha256"]:
                    raise Problem("checksum_mismatch", "Uploaded file does not match its SHA-256 digest")
                return digest, inspect_media(path)

            digest, metadata = await asyncio.to_thread(validate)
            with self.store.db:
                self.store.db.execute("UPDATE media SET sha256=?,metadata=?,status='ready' WHERE id=?", (
                    digest, canonical(metadata), mid))
            return {"media_id": mid, **metadata}

    async def fetch(self, account, url, name="attachment", max_bytes=None):
        public_url(url)
        if max_bytes is not None and (not isinstance(max_bytes, int) or not 0 < max_bytes <= MAX_FILE):
            raise Problem("invalid_size", "Media download limit must be between 1 byte and 8 GiB")
        resolver = PublicResolver()
        connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False, limit=2)
        timeout = aiohttp.ClientTimeout(total=7200, sock_connect=20, sock_read=60)
        mid = None
        try:
            async with self.transfers, aiohttp.ClientSession(connector=connector, timeout=timeout, trust_env=False) as session:
                for _ in range(4):
                    public_url(url)
                    async with session.get(url, allow_redirects=False) as response:
                        if response.status in {301, 302, 303, 307, 308}:
                            url = urljoin(url, response.headers.get("Location", ""))
                            continue
                        if response.status != 200:
                            raise Problem("media_download_failed", f"Media source returned HTTP {response.status}; refresh expired links")
                        length = response.content_length
                        if max_bytes is not None and length is not None and length > max_bytes:
                            raise Problem("media_too_large", "Media exceeds the allowed download size")
                        reserved = length if length is not None else (max_bytes or MAX_FILE)
                        upload = self.begin(account, name, reserved)
                        mid = upload["media_id"]
                        with self.store.db:
                            self.store.db.execute("UPDATE media SET importing=1 WHERE id=?", (mid,))
                        count = 0
                        with (self.store.media_directory / mid).open("wb") as f:
                            async for chunk in response.content.iter_chunked(CHUNK):
                                count += len(chunk)
                                if count > reserved:
                                    raise Problem("media_too_large", "Media exceeded the reserved upload size")
                                f.write(chunk)
                            f.flush()
                            os.fsync(f.fileno())
                        if not count or (length is not None and count != length):
                            raise Problem("incomplete_download", "Media download was incomplete")
                        with self.store.db:
                            self.store.db.execute("UPDATE media SET size=?,offset=?,importing=0 WHERE id=?", (count, count, mid))
                        return await self.finish(mid, account)
                raise Problem("too_many_redirects", "Media source redirected too many times")
        except BaseException:
            if mid:
                (self.store.media_directory / mid).unlink(missing_ok=True)
                with self.store.db:
                    self.store.db.execute("DELETE FROM media WHERE id=?", (mid,))
            raise
        finally:
            await resolver.close()

    def cleanup(self):
        # Preserve media referenced by an in-flight publication.
        active = self.store.db.execute("SELECT payload FROM operations WHERE state NOT IN ('succeeded','failed','partial','unknown')").fetchall()
        for row in self.store.db.execute("SELECT id FROM media WHERE expires<?", (time.time(),)).fetchall():
            if any(row["id"] in op["payload"] for op in active):
                continue
            if self.locks.get(row["id"], asyncio.Lock()).locked():
                continue
            (self.store.media_directory / row["id"]).unlink(missing_ok=True)
            with self.store.db:
                self.store.db.execute("DELETE FROM media WHERE id=?", (row["id"],))
            self.locks.pop(row["id"], None)
        with self.store.db:
            self.store.db.execute("DELETE FROM drafts WHERE expires<?", (time.time(),))
