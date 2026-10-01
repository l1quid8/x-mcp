import asyncio
import hashlib
import json
import time
import warnings

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message="pkg_resources is deprecated as an API.*", category=UserWarning)
    from twitter_text import parse_tweet
    from twitter_text.config import config as text_config

from .backend import XBackend, article_blocks, classify
from .core import Problem, Publication, TERMINAL, canonical, identifier


class Publisher:
    def __init__(self, store, backend_factory=XBackend):
        self.store, self.backend_factory = store, backend_factory
        self.account_locks = {}
        self.tasks = set()

    def validate(self, account, publication):
        record = self.store.account(account)
        caps = json.loads(record["capabilities"])
        blockers = []
        if publication.kind == "article":
            if caps.get("articles") is not True:
                blockers.append({"code": "account_capability_unverified" if caps.get("articles") is None else "account_restricted",
                                 "message": "Article access is not verified for this account"})
            if not self.backend_factory.articles_ready:
                blockers.append({"code": "article_backend_unverified", "message": "Article backend has not been validated"})
            article_blocks(publication.article.markdown)
            attachments = list(publication.article.media)
            ids = [a.media_id for a in attachments]
            if publication.article.cover_media_id:
                ids.append(publication.article.cover_media_id)
            for mid in ids:
                self.ready_media(mid, account, caps)
        for post in publication.posts:
            limit = caps.get("text_limit", 280) if post.long_post else 280
            parsed = parse_tweet(post.text, {**text_config["defaults"], "max_weighted_tweet_length": limit})
            if parsed.weightedLength > limit:
                raise Problem("text_too_long", f"Text exceeds the configured {limit}-character account limit")
            if post.text and not parsed.valid:
                raise Problem("invalid_text", "Text contains characters that X does not permit")
            if post.long_post and caps.get("long_posts") is not True:
                raise Problem("account_restricted", "Long-post access has not been verified for this account")
            metadata = [self.ready_media(a.media_id, account, caps) for a in post.attachments]
            if any(m["kind"] == "gif" for m in metadata) and len(metadata) != 1:
                raise Problem("invalid_media_combination", "An animated GIF must be the only attachment")
            if sum(m["kind"] == "video" for m in metadata) > 1:
                raise Problem("invalid_media_combination", "Use at most one video per post")
        return record, blockers

    def ready_media(self, mid, account, caps):
        row = self.store.media(mid, account)
        if row["status"] != "ready":
            raise Problem("media_not_ready", "Finish media staging before previewing")
        metadata = json.loads(row["metadata"])
        size = row["size"]
        if metadata["kind"] == "video":
            if size > caps["video_max_bytes"] or metadata["duration"] > caps["video_max_seconds"]:
                raise Problem("account_media_limit", "Video exceeds the verified/configured limits for this account")
        elif size > (15 * 1024**2 if metadata["kind"] == "gif" else 5 * 1024**2):
            raise Problem("account_media_limit", "Image or GIF exceeds its supported upload limit")
        return metadata

    def preview(self, account, publication):
        record, blockers = self.validate(account, publication)
        payload = canonical(publication.model_dump())
        digest = hashlib.sha256((account + "\n" + payload).encode()).hexdigest()
        did = identifier()
        with self.store.db:
            self.store.db.execute("INSERT INTO drafts VALUES (?,?,?,?,?)", (did, account, payload, digest, time.time() + 86400))
        return {"draft_id": did, "account_id": account, "username": record["username"],
                "content": json.loads(payload), "publishable": not blockers, "blockers": blockers,
                "expires_in_seconds": 86400, "published": False}

    def submit(self, account, draft_id, idempotency_key):
        if not 8 <= len(idempotency_key) <= 128:
            raise Problem("invalid_idempotency_key", "Use a stable request key of 8–128 characters")
        self.store.account(account)
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            old = self.store.db.execute("SELECT * FROM operations WHERE account=? AND idempotency=?", (account, idempotency_key)).fetchone()
            draft = self.store.db.execute("SELECT * FROM drafts WHERE id=? AND account=?", (draft_id, account)).fetchone()
            if old:
                if draft and old["hash"] != draft["hash"]:
                    raise Problem("idempotency_conflict", "This request key already belongs to different content")
                return self.status(old["id"])
            if not draft or draft["expires"] <= time.time():
                raise Problem("draft_unavailable", "Draft is missing, expired, or belongs to another account")
            publication = Publication.model_validate_json(draft["payload"])
            _, blockers = self.validate(account, publication)
            if blockers:
                raise Problem(blockers[0]["code"], blockers[0]["message"])
            opid = identifier()
            now = time.time()
            result = {"posts": [], "unsent_entries": len(publication.posts), "error": None}
            self.store.db.execute("INSERT INTO operations VALUES (?,?,?,?,?,'queued',?,'queued',?,?)", (
                opid, account, idempotency_key, draft["hash"], draft["payload"], canonical(result), now, now))
        task = asyncio.create_task(self.run(opid))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return self.status(opid)

    def status(self, opid):
        row = self.store.operation(opid)
        return {"operation_id": opid, "account_id": row["account"], "state": row["state"],
                "phase": row["phase"], **json.loads(row["result"])}

    async def run(self, opid):
        row = self.store.operation(opid)
        lock = self.account_locks.setdefault(row["account"], asyncio.Lock())
        async with lock:
            backend = None
            result = json.loads(row["result"])
            submitting = False
            identity_verified = False
            try:
                publication = Publication.model_validate_json(row["payload"])
                account, blockers = self.validate(row["account"], publication)
                if blockers:
                    raise Problem(blockers[0]["code"], blockers[0]["message"])
                backend = self.backend_factory(self.store.session(row["account"]))
                self.store.update_operation(opid, "running", result, "identity_check")
                identity = await backend.identity()
                if identity["id"] != row["account"]:
                    raise Problem("identity_mismatch", "X session belongs to a different account; reconnect it")
                identity_verified = True
                with self.store.db:
                    self.store.db.execute("UPDATE accounts SET checked=? WHERE id=?", (time.time(), row["account"]))
                if publication.kind == "article":
                    async def checkpoint(stage, receipt):
                        result["article"] = receipt
                        self.store.update_operation(opid, "running", result, stage)
                    submitting = True
                    result["article"] = await backend.create_article(publication.article.model_dump(), checkpoint)
                previous = None
                for index, post in enumerate(publication.posts):
                    self.store.account(row["account"])
                    submitting = False
                    self.store.update_operation(opid, "running", result, f"preparing_entry_{index + 1}")
                    media_ids = []
                    for attachment in post.attachments:
                        media = self.store.media(attachment.media_id, row["account"])
                        metadata = json.loads(media["metadata"])
                        remote_id = await backend.upload(self.store.media_directory / attachment.media_id, metadata,
                                                       long_video=metadata.get("duration", 0) > 140)
                        await backend.metadata(remote_id, attachment.alt_text)
                        media_ids.append(remote_id)
                    poll = await backend.poll(post.poll.choices, post.poll.duration_minutes) if post.poll else None
                    self.store.update_operation(opid, "running", result, f"submitting_entry_{index + 1}")
                    submitting = True
                    receipt = await backend.create_post(post.model_dump(), media_ids, poll, previous or post.reply_to)
                    receipt["verified"] = False
                    result["posts"].append(receipt)
                    result["unsent_entries"] = len(publication.posts) - index - 1
                    self.store.update_operation(opid, "running", result, f"verifying_entry_{index + 1}")
                    submitting = False
                    receipt["verified"] = await backend.verify_post(receipt["id"], row["account"])
                    if not receipt["verified"]:
                        raise Problem("verification_failed", "X returned a post ID but read-back verification failed; do not repost")
                    caps = json.loads(self.store.account(row["account"])["capabilities"])
                    verified = caps.setdefault("verified_formats", {})
                    formats = {"long_post" if post.long_post else "text"}
                    if previous or post.reply_to:
                        formats.add("reply")
                    if previous:
                        formats.add("thread")
                    if post.quote_id:
                        formats.add("quote")
                    if post.poll:
                        formats.add("poll")
                    for attachment in post.attachments:
                        md = json.loads(self.store.media(attachment.media_id, row["account"])["metadata"])
                        formats.add(md["kind"])
                        if md.get("duration", 0) > 140:
                            formats.add("long_video")
                    for fmt in formats:
                        verified[fmt] = {"post_id": receipt["id"], "checked_at": time.time()}
                    with self.store.db:
                        self.store.db.execute("UPDATE accounts SET capabilities=? WHERE id=?", (canonical(caps), row["account"]))
                    previous = receipt["id"]
                    self.store.update_operation(opid, "running", result, f"verified_entry_{index + 1}")
                self.store.update_operation(opid, "succeeded", result, "complete")
            except asyncio.CancelledError:
                result["error"] = {"code": "interrupted", "message": "Service stopped during publication; inspect existing receipts before taking action"}
                self.store.update_operation(opid, "unknown", result, "interrupted")
                raise
            except Exception as exc:
                code, message = (exc.code, exc.message) if isinstance(exc, Problem) else classify(exc)
                result["error"] = {"code": code, "message": message}
                # Definitive application rejections are safe failures; transport
                # errors during submission must never be converted into retries.
                definite = code in {"x_rejected", "rate_limited", "session_or_account_restricted"}
                state = "unknown" if submitting and not definite else ("partial" if result["posts"] else "failed")
                self.store.update_operation(opid, state, result, "stopped")
            finally:
                if backend:
                    try:
                        if identity_verified and hasattr(backend, "session_snapshot"):
                            current = self.store.account(row["account"])
                            self.store.save_account(row["account"], current["username"], backend.session_snapshot(), json.loads(current["capabilities"]))
                    except Exception:
                        pass
                    try:
                        await backend.close()
                    except Exception:
                        pass

    async def close(self):
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
