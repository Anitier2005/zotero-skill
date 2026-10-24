# -*- coding: utf-8 -*-
"""
zapi.py — Zotero 本地 HTTP API 客户端（写入通道）。

为什么写操作不走 SQLite
----------------------
Zotero 客户端运行时把库缓存放在内存里，并通过数据库里的 version / synced /
clientDateModified 等字段与同步服务对账。绕过它直接改 zotero.sqlite 会：

* 让客户端的缓存与磁盘不一致，退出时可能被覆盖回去（改动"消失"）；
* 版本号不对，下次同步把改动判为冲突或直接丢弃；
* 漏掉 Zotero 自己的触发器与全文索引维护，轻则索引失效，重则库损坏。

所以本模块只做一件事：通过 Zotero 官方本地 API 发请求，让 Zotero 自己落盘。

前置条件
--------
1. Zotero 正在运行；
2. 设置 → 高级 → 勾选「允许其他应用程序在此计算机上与 Zotero 通信」；
3. 版本 ≥ 10（10 之前本地 API 只读，写请求会被拒绝）。

作者：zotero-local skill
"""

import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:23119/api"
API_HEADER = {"Zotero-API-Version": "3"}
APP_NAME = "zotero-local skill"
AUTH_TIMEOUT = 120  # 授权要等用户在 Zotero 弹窗上点按钮，给足时间

# 本地写入能力要求的 Zotero 大版本
MIN_WRITE_MAJOR = 10


class ApiError(RuntimeError):
    """本地 API 调用失败。status 为 HTTP 状态码，None 表示没拿到响应。"""

    def __init__(self, message, status=None, body=None, hint=None):
        super().__init__(message)
        self.status = status
        self.body = body
        self.hint = hint


class NotRunning(ApiError):
    """连不上 127.0.0.1:23119 —— Zotero 没开。"""


class Disabled(ApiError):
    """403 —— 本地 API 未在设置里启用。"""


class Unsupported(ApiError):
    """404/405 —— 当前 Zotero 版本不支持写入。"""


def _opener():
    # 关键：显式禁用系统代理。不少开发机会设置 HTTP_PROXY，urllib 默认会把
    # 发往 127.0.0.1 的请求也塞给代理，结果是连本地服务也失败。
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def key_cache_path():
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return os.path.join(base, "zotero-local", "write-key.json")


class LocalApi:
    def __init__(self, base=DEFAULT_BASE, user="0", timeout=20, app_name=APP_NAME):
        self.base = (base or DEFAULT_BASE).rstrip("/")
        self.user = str(user)
        self.timeout = timeout
        self.app_name = app_name
        self._opener = _opener()
        self._server_id = None
        self._key = None
        self._remember = False

    # ---------------- 底层请求 ----------------

    def _url(self, path):
        return "{}/{}".format(self.base, path.lstrip("/"))

    def request(self, method, path, payload=None, headers=None, auth=False, timeout=None):
        hdrs = dict(API_HEADER)
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if headers:
            hdrs.update({k: v for k, v in headers.items() if v})
        if auth:
            if self._server_id:
                hdrs["Zotero-Server-ID"] = self._server_id
            if self._key:
                hdrs["Zotero-API-Key"] = self._key

        req = urllib.request.Request(self._url(path), data=data, headers=hdrs, method=method)
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
                body = _maybe_json(raw)
                return resp.status, dict(resp.headers), body
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            body = _maybe_json(raw)
            raise _translate_http_error(exc.code, body)
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise NotRunning(
                "连不上 Zotero 本地 API（{}）：{}".format(self.base, reason),
                hint="请先启动 Zotero 客户端，再重试。",
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise NotRunning(
                "连接 Zotero 本地 API 超时：{}".format(exc),
                hint="确认 Zotero 正在运行且没有卡死。",
            ) from exc

    # ---------------- 能力探测 ----------------

    def ping(self):
        """读一次 /api/，拿到 API 版本与 Server-ID。读请求不需要授权。"""
        _, headers, _ = self.request("GET", "/")
        self._server_id = headers.get("Zotero-Server-ID")
        return {
            "apiVersion": headers.get("Zotero-API-Version"),
            "schemaVersion": headers.get("Zotero-Schema-Version"),
            "serverId": self._server_id,
        }

    # ---------------- 授权 ----------------

    def _load_cached_key(self):
        if not self._server_id:
            return None
        try:
            with open(key_cache_path(), "r", encoding="utf-8") as fh:
                store = json.load(fh)
        except (OSError, ValueError):
            return None
        entry = store.get(self._server_id) or {}
        return entry.get("key")

    def _save_cached_key(self, key):
        if not self._server_id or not key:
            return
        path = key_cache_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    store = json.load(fh)
            except (OSError, ValueError):
                store = {}
            store[self._server_id] = {"key": key, "app": self.app_name}
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(store, fh, ensure_ascii=False, indent=2)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except OSError:
            pass

    def authorize(self, use_cache=True):
        """向 Zotero 弹窗申请写入密钥。用户在弹窗里点『允许』或『始终允许』。

        返回 True 表示拿到了可用密钥；用户点了『拒绝』则抛 ApiError。
        """
        if not self._server_id:
            self.ping()

        if use_cache and not self._key:
            cached = self._load_cached_key()
            if cached:
                self._key = cached

        if self._key:
            return True

        try:
            _, _, body = self.request(
                "POST",
                "/local/authorize",
                payload={"appName": self.app_name},
                headers={"Zotero-Server-ID": self._server_id or ""},
                timeout=AUTH_TIMEOUT,  # 等用户点弹窗，不能用普通请求的超时
            )
        except Unsupported:
            raise
        except ApiError as exc:
            if exc.status == 403:
                raise ApiError(
                    "Zotero 拒绝了写入授权申请。",
                    status=403,
                    hint="需要在 Zotero 弹出的窗口里点『允许』或『始终允许』。",
                ) from exc
            if exc.status == 429:
                raise ApiError(
                    "授权请求过于频繁，被 Zotero 限流。",
                    status=429,
                    hint="一分钟内最多触发 5 次授权窗口，稍等片刻再试。",
                ) from exc
            raise

        if not isinstance(body, dict) or not body.get("key"):
            raise ApiError("授权响应异常：{}".format(body))

        self._key = body["key"]
        self._remember = bool(body.get("remember"))
        if self._remember:
            self._save_cached_key(self._key)
        return True

    def _ensure_key(self):
        if not self._key:
            self.authorize()

    # ---------------- 读写封装 ----------------

    def get_item(self, key):
        _, _, body = self.request("GET", "/users/{}/items/{}".format(self.user, key))
        if not isinstance(body, dict):
            raise ApiError("条目 {} 不存在或不可读。".format(key), status=404)
        return body

    def get_item_children(self, key):
        _, _, body = self.request("GET", "/users/{}/items/{}/children".format(self.user, key))
        return body or []

    def list_collections(self):
        _, _, body = self.request("GET", "/users/{}/collections".format(self.user))
        return body or []

    def create_items(self, items):
        """items 为符合 Zotero Web API 的条目 JSON 列表。返回 (成功, 失败)。"""
        self._ensure_key()
        _, _, body = self.request(
            "POST", "/users/{}/items".format(self.user), payload=items, auth=True
        )
        return _split_result(body)

    def update_item(self, key, payload):
        """payload 为要覆盖的字段集合；版本冲突时 Zotero 会返回 412。"""
        self._ensure_key()
        current = self.get_item(key)
        try:
            _, _, body = self.request(
                "PATCH",
                "/users/{}/items/{}".format(self.user, key),
                payload=payload,
                headers={"If-Unmodified-Since-Version": str(current.get("version", 0))},
                auth=True,
            )
        except ApiError as exc:
            if exc.status == 401:
                # 密钥可能是一次性的，被上一次写入消费掉了，重新授权一次
                self._key = None
                self.authorize(use_cache=False)
                _, _, body = self.request(
                    "PATCH",
                    "/users/{}/items/{}".format(self.user, key),
                    payload=payload,
                    headers={"If-Unmodified-Since-Version": str(current.get("version", 0))},
                    auth=True,
                )
            elif exc.status == 412:
                raise ApiError(
                    "条目 {} 在本次操作前已被其他程序修改，为避免覆盖已中止。".format(key),
                    status=412,
                    hint="重新读取该条目后再试一次。",
                ) from exc
            else:
                raise
        return body

    def library_version(self):
        """取库当前版本号，供 DELETE 类请求做并发前提校验。"""
        _, headers, _ = self.request("GET", "/users/{}/items?limit=1".format(self.user))
        try:
            return int(headers.get("Last-Modified-Version") or 0)
        except (TypeError, ValueError):
            return 0

    def trash_item(self, key):
        """移入回收站（软删除，可在 Zotero 中还原）。

        注意：本地 API 的 DELETE 走的是 obj.eraseTx()，是永久抹除而不是回收站，
        这一点与 zotero.org Web API 的语义不同。软删除必须 PATCH deleted=1。
        """
        return self.update_item(key, {"deleted": 1})

    def delete_item(self, key):
        """永久删除（不可恢复）。本地 API 会调用 Zotero 的 eraseTx()。

        DELETE 与 PATCH 一样必须带 If-Unmodified-Since-Version，否则返回
        428 Precondition Required。
        """
        self._ensure_key()
        current = self.get_item(key)
        headers = {"If-Unmodified-Since-Version": str(current.get("version", 0))}
        try:
            status, _, _ = self.request(
                "DELETE", "/users/{}/items/{}".format(self.user, key),
                headers=headers, auth=True,
            )
        except ApiError as exc:
            if exc.status == 401:
                self._key = None
                self.authorize(use_cache=False)
                status, _, _ = self.request(
                    "DELETE", "/users/{}/items/{}".format(self.user, key),
                    headers=headers, auth=True,
                )
            elif exc.status == 412:
                raise ApiError(
                    "条目 {} 在本次操作前已被其他程序修改，为避免误删已中止。".format(key),
                    status=412,
                    hint="重新读取该条目后再试一次。",
                ) from exc
            else:
                raise
        return status

    def list_trash(self):
        _, _, body = self.request("GET", "/users/{}/items/trash".format(self.user))
        return body or []

    def empty_trash(self):
        """清空回收站 —— 不可逆。"""
        self._ensure_key()
        status, _, _ = self.request(
            "DELETE", "/users/{}/items/trash".format(self.user),
            headers={"If-Unmodified-Since-Version": str(self.library_version())},
            auth=True,
        )
        return status


def _maybe_json(raw):
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace")


def _split_result(body):
    if isinstance(body, dict) and ("successful" in body or "failed" in body):
        return body.get("successful") or {}, body.get("failed") or {}
    if isinstance(body, dict):
        return body, {}
    return {}, {"_": body}


def _translate_http_error(code, body):
    message = None
    hint = None
    if isinstance(body, dict):
        message = body.get("message") or body.get("error")
        if body.get("denied"):
            message = "Zotero 拒绝了授权请求（用户点了『拒绝』）。"

    if code == 403:
        return Disabled(
            "Zotero 本地 API 未启用（403）。",
            status=code,
            body=body,
            hint="在 Zotero 里打开 设置 → 高级 → 勾选「允许其他应用程序在此计算机上与 Zotero 通信」，然后重试。",
        )
    if code in (404, 405):
        return Unsupported(
            "当前 Zotero 不提供该接口（HTTP {}）。".format(code),
            status=code,
            body=body,
            hint="本地 API 的写入能力（POST/PATCH/DELETE）自 Zotero 10 起才提供；"
                 "请升级到 Zotero 10 或更高版本。",
        )
    return ApiError(
        message or "本地 API 返回 HTTP {}".format(code),
        status=code,
        body=body,
        hint=hint,
    )


def probe(base=None):
    """探测写入通道可用性，返回结构化结果而非抛异常。供 CLI 做友好提示。"""
    api = LocalApi(base=base or DEFAULT_BASE, timeout=5)
    try:
        info = api.ping()
    except NotRunning as exc:
        return {"ok": False, "stage": "not_running", "message": str(exc), "hint": exc.hint}
    except Disabled as exc:
        return {"ok": False, "stage": "disabled", "message": str(exc), "hint": exc.hint}
    except ApiError as exc:
        return {"ok": False, "stage": "error", "message": str(exc), "hint": exc.hint}

    info["ok"] = True
    info["stage"] = "reachable"
    return info


if __name__ == "__main__":  # 便于手工排查：python zapi.py
    print(json.dumps(probe(), ensure_ascii=False, indent=2))
    sys.exit(0)
