# Copyright (c) 2025 AnonX
# Licensed under the MIT License.

"""Safe Redis distributed singleflight: lock + heartbeat + result broadcast.

Falls back to process-local SingleFlight when Redis is unavailable.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from AnonX_3 import config
from AnonX_3.core.downloader.singleflight import SingleFlight

T = TypeVar("T")
logger = logging.getLogger("AnonX_3")

# Lua: delete key only if value matches owner token
_LUA_UNLOCK = """
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("del", KEYS[1])
else
  return 0
end
"""


class RedisSingleFlight:
    def __init__(self, name: str = "media", local: SingleFlight | None = None) -> None:
        self.name = name
        self._local = local or SingleFlight(f"local:{name}")
        self._redis = None
        self._owner_token: dict[str, str] = {}
        self._ttl = max(30, int(getattr(config, "REDIS_LOCK_TTL_SEC", 120) or 120))
        self._result_ttl = max(
            30, int(getattr(config, "REDIS_RESULT_TTL_SEC", 300) or 300)
        )

    async def _client(self):
        if self._redis is not None:
            return self._redis
        url = (getattr(config, "REDIS_URL", "") or "").strip()
        if not url:
            return None
        try:
            import redis.asyncio as redis_async

            self._redis = redis_async.from_url(
                url, encoding="utf-8", decode_responses=True
            )
            await self._redis.ping()
            logger.info("Redis singleflight connected name=%s", self.name)
            return self._redis
        except Exception as ex:
            logger.warning("Redis singleflight unavailable, using memory: %s", ex)
            self._redis = None
            return None

    def _lock_key(self, key: str) -> str:
        return f"AnonX:lock:{self.name}:{key}"

    def _result_key(self, key: str) -> str:
        return f"AnonX:job:{self.name}:{key}"

    def _channel(self, key: str) -> str:
        return f"AnonX:ch:{self.name}:{key}"

    async def _unlock(self, client, lock_key: str, token: str) -> None:
        try:
            await client.eval(_LUA_UNLOCK, 1, lock_key, token)
        except Exception:
            try:
                cur = await client.get(lock_key)
                if cur == token:
                    await client.delete(lock_key)
            except Exception:
                pass

    async def _heartbeat(self, client, lock_key: str, token: str, stop: asyncio.Event):
        interval = max(5.0, self._ttl / 3.0)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass
            try:
                cur = await client.get(lock_key)
                if cur == token:
                    await client.expire(lock_key, self._ttl)
                else:
                    break
            except Exception:
                break

    async def _publish_result(self, client, key: str, payload: dict) -> None:
        rkey = self._result_key(key)
        try:
            await client.set(rkey, json.dumps(payload), ex=self._result_ttl)
            await client.publish(self._channel(key), json.dumps(payload))
        except Exception as ex:
            logger.debug("redis result publish failed: %s", ex)

    async def do(
        self,
        key: str,
        factory: Callable[[], Awaitable[T]],
        *,
        timeout: float | None = None,
    ) -> T:
        if self._local.is_running(key):
            return await self._local.do(key, factory, timeout=timeout)

        client = await self._client()
        if client is None:
            return await self._local.do(key, factory, timeout=timeout)

        # Existing result?
        try:
            raw = await client.get(self._result_key(key))
            if raw:
                data = json.loads(raw)
                if data.get("ok") and data.get("path"):
                    # Caller factory often returns path string
                    return data.get("path")  # type: ignore[return-value]
        except Exception:
            pass

        lock_key = self._lock_key(key)
        token = uuid.uuid4().hex
        deadline = time.time() + (
            timeout if timeout is not None else float(self._ttl)
        )

        async def _run_owner() -> T:
            stop_hb = asyncio.Event()
            hb = asyncio.create_task(
                self._heartbeat(client, lock_key, token, stop_hb)
            )
            try:
                result = await factory()
                path = result if isinstance(result, str) else getattr(result, "local_path", None)
                await self._publish_result(
                    client,
                    key,
                    {"ok": True, "path": path, "ts": time.time()},
                )
                return result
            except Exception as ex:
                await self._publish_result(
                    client,
                    key,
                    {"ok": False, "err": str(ex)[:200], "ts": time.time()},
                )
                raise
            finally:
                stop_hb.set()
                hb.cancel()
                with contextlib.suppress(Exception):
                    await hb
                await self._unlock(client, lock_key, token)
                self._owner_token.pop(key, None)

        try:
            acquired = await client.set(lock_key, token, nx=True, ex=self._ttl)
        except Exception as ex:
            logger.warning("Redis SET lock failed, local fallback: %s", ex)
            return await self._local.do(key, factory, timeout=timeout)

        if acquired:
            self._owner_token[key] = token
            return await self._local.do(key, _run_owner, timeout=timeout)

        # Waiter: poll result key / lock free
        while time.time() < deadline:
            if self._local.is_running(key):
                return await self._local.do(
                    key, factory, timeout=max(0.1, deadline - time.time())
                )
            try:
                raw = await client.get(self._result_key(key))
                if raw:
                    data = json.loads(raw)
                    if data.get("ok") and data.get("path"):
                        return data.get("path")  # type: ignore[return-value]
                    if data.get("ok") is False:
                        # Owner failed — try acquire ourselves
                        break
            except Exception:
                pass
            try:
                exists = await client.exists(lock_key)
            except Exception:
                break
            if not exists:
                try:
                    acquired = await client.set(lock_key, token, nx=True, ex=self._ttl)
                except Exception:
                    break
                if acquired:
                    self._owner_token[key] = token
                    return await self._local.do(key, _run_owner, timeout=timeout)
            await asyncio.sleep(0.2)

        logger.warning("Redis singleflight wait timeout key=%s; local run", key)
        return await self._local.do(key, factory, timeout=timeout)

    def get_task(self, key: str):
        return self._local.get_task(key)

    def is_running(self, key: str) -> bool:
        return self._local.is_running(key)

    def cancel(self, key: str) -> bool:
        return self._local.cancel(key)

    def stats(self) -> dict[str, Any]:
        st = self._local.stats()
        st["backend"] = "redis" if self._redis is not None else "memory"
        return st

    async def cleanup_stale_locks(self, max_age_sec: float = 300.0) -> int:
        """Scan and remove Redis locks older than max_age_sec without active heartbeat.
        
        Returns count of cleaned stale locks.
        """
        client = await self._client()
        if client is None:
            return 0
        cleaned = 0
        try:
            pattern = f"AnonX:lock:{self.name}:*"
            cursor = 0
            now = time.time()
            while True:
                cursor, keys = await client.scan(cursor, match=pattern, count=50)
                for lock_key in keys:
                    try:
                        ttl = await client.ttl(lock_key)
                        if ttl is None or ttl < 0:
                            # No TTL or persisted — skip
                            continue
                        # Original TTL was self._ttl; if remaining > original, skip
                        # If TTL is very short (< 10% original), the owner likely crashed
                        owner = await client.get(lock_key)
                        if not owner:
                            # Key expired between SCAN and GET
                            continue
                        # If the lock is near expiry (< 15% of original TTL),
                        # owner's heartbeat probably stopped → stale
                        if ttl < max(5, self._ttl * 0.15):
                            # Safety: check if result exists (job completed but unlock failed)
                            rkey = lock_key.replace("AnonX:lock:", "AnonX:job:", 1)
                            result_exists = await client.exists(rkey)
                            if result_exists:
                                # Job completed; lock just wasn't cleaned
                                await client.delete(lock_key)
                                cleaned += 1
                            elif ttl < 3:
                                # Near-dead lock with no result — clean it
                                await client.delete(lock_key)
                                cleaned += 1
                    except Exception:
                        continue
                if cursor == 0:
                    break
        except Exception as ex:
            logger.debug("stale lock scan failed: %s", ex)
        if cleaned:
            logger.info("cleaned %d stale redis lock(s)", cleaned)
        return cleaned
