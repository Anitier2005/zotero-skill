# 写入通道：Zotero 本地 API 协议

写操作只走这条通道。本文记录协议细节与坑，供扩展脚本时参考。

## 为什么不能直接写 SQLite

Zotero 客户端运行时把库缓存在内存里，并用 `version` / `synced` / `clientDateModified` 与同步服务对账。绕过客户端直接改 `zotero.sqlite`：

- 内存缓存与磁盘不一致，客户端退出时可能覆盖回去，改动"凭空消失"；
- 版本号不对，下次同步判为冲突或直接丢弃；
- 漏掉 Zotero 自己的触发器与全文索引维护，轻则索引失效，重则库损坏。

本地 API 的写入是"普通本地改动"：界面立即刷新，下次同步自动上传。这是唯一安全路径。

## 端点

基址 `http://127.0.0.1:23119/api`，用户前缀 `/users/0`。

| 操作 | 方法 + 路径 |
|---|---|
| 列条目 | `GET /users/0/items` |
| 单条 | `GET /users/0/items/<KEY>` |
| 子项 | `GET /users/0/items/<KEY>/children` |
| 新增 | `POST /users/0/items`（body 为数组） |
| 修改 | `PATCH /users/0/items/<KEY>` |
| 删除（入回收站） | `PATCH /users/0/items/<KEY>`，body `{"deleted": 1}` |
| 删除（永久，不可恢复） | `DELETE /users/0/items/<KEY>` |
| 回收站列表 | `GET /users/0/items/trash` |
| 清空回收站 | `DELETE /users/0/items/trash` |
| 分类 | `GET/POST /users/0/collections` |

## 必需请求头

```
Zotero-API-Version: 3
Zotero-Server-ID: <从任意 GET 响应的同名响应头取到>   # 写请求必带，否则 428
Zotero-API-Key: <授权拿到的密钥>                      # 写请求必带，否则 401
If-Unmodified-Since-Version: <条目的 version>          # PATCH 必带，冲突时 412
Content-Type: application/json
```

`Server-ID` 标识"这一个数据库"。数据目录换了 ID 就变，此时旧密钥与缓存版本全部作废（读请求带错 ID 会 412）。

## 授权流程

写入密钥不能预先创建，必须运行时向用户申请：

```http
POST /api/local/authorize
Zotero-Server-ID: <serverID>
Content-Type: application/json

{"appName": "zotero-local skill"}
```

Zotero 弹窗，三个按钮：

- **允许** → 返回 `{"key": "...", "remember": false}`，**一次性**，用完即废；
- **始终允许** → `remember: true`，密钥可长期复用；
- **拒绝** → `403` + `{"denied": true}`。

一分钟内最多触发 5 次弹窗，超了 `429` + `Retry-After`。

本 skill 的行为：`remember: true` 时把密钥按 Server-ID 存到 `%LOCALAPPDATA%\zotero-local\write-key.json`（POSIX 权限 600），下次直接复用、不再弹窗；一次性密钥不落盘。撤销：Zotero 设置 → 高级 →「Clear Write Authorizations」，或直接删该文件。

## 错误码对照

| 码 | 含义 | 处理 |
|---|---|---|
| 401 | 无密钥/密钥失效（一次性密钥被消费） | 重新授权一次 |
| 403 | 本地 API 未启用；或用户点了拒绝 | 让用户去设置里开启 |
| 404/405 | 该接口不存在 —— Zotero 9 及更早的本地 API 只读 | 升级到 Zotero 10+ |
| 412 | `If-Unmodified-Since-Version` 不匹配，或 Server-ID 不符 | 重新读取后再改，禁止硬覆盖 |
| 428 | 写请求缺 `Zotero-Server-ID` | 先 GET 一次取头 |
| 429 | 授权弹窗过频 | 等 `Retry-After` |

## 语义细节

**PATCH 是"提供即覆盖"**。数组是整体替换，不是追加——所以加标签必须先读出 `tags` 全量、合并、再整体回写，否则会把原有标签冲掉。

**`DELETE /items/<KEY>` 是永久抹除，不是移入回收站**。源码里 `deleteSingleObject` 直接调用 `obj.eraseTx()`，与 zotero.org Web API 的语义相反。要软删除（进回收站、可还原）必须 `PATCH {"deleted": 1}`。`DELETE /items/trash` 清的是整个回收站，不可逆，必须单独向用户确认。

**DELETE 类请求必须带 `If-Unmodified-Since-Version`**（单条删除带该条目的 version，清空回收站带库的 `Last-Modified-Version`）。缺这个头返回 428，与缺 `Zotero-Server-ID` 的报错码相同，排查时别混淆。

**只读字段**：`key` / `version` / `links` / `meta` / `synced` 不要回传；`dateAdded` / `dateModified` 由客户端维护，改它们没有意义。

**暂不支持**：附件上传要走三阶段流程（`POST -> 上传字节 -> POST upload=<key>`），本 skill 未实现；需要加附件请在 Zotero 界面里拖拽。部分二进制差异上传（`PATCH .../file`）在本地 API 上直接返回 405。

**条目类型与字段**：`GET /api/itemTypes`、`GET /api/itemTypeFields?itemType=xxx` 可查合法字段，返回的是当前界面语言的名字。字段名传错时 Zotero 返回 400 并说明原因，照提示改即可。
