# Zotero 库结构速查

脚本命令覆盖不到时，可以自己写 SQL。前提：**先在副本上操作**，绝不直接打开 `zotero.sqlite`。

## 安全复制模板

```python
import os, shutil, sqlite3, tempfile

src = os.path.join(DATA_DIR, "zotero.sqlite")
dst = os.path.join(tempfile.mkdtemp(), "zotero.sqlite")
shutil.copyfile(src, dst)
# -wal / -journal 若存在也必须一起复制，否则快照可能不完整
for suffix in ("-wal", "-journal"):
    if os.path.isfile(src + suffix):
        shutil.copyfile(src + suffix, dst + suffix)

conn = sqlite3.connect(dst)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA query_only = 1")   # 只读保险
```

只复制主库、漏掉 `-wal` 时，快照会停留在上一次 checkpoint 的状态，表现为"Zotero 里明明有，查出来没有"。

## 常用表链

| 需求 | 表关系 |
|---|---|
| 条目类型 | `items` ⋈ `itemTypes`（`items.key` 即 8 位 KEY，`items.libraryID` 区分个人库/群体库） |
| 字段值 | `items` ⋈ `itemData` ⋈ `fields` ⋈ `itemDataValues` |
| 作者 | `items` ⋈ `itemCreators` ⋈ `creators` ⋈ `creatorTypes`，按 `orderIndex` 排序 |
| 标签 | `itemTags` ⋈ `tags` |
| 分类归属 | `collectionItems` ⋈ `collections`（`parentCollectionID` 构成层级） |
| 附件 | `itemAttachments`：`parentItemID` 指向文献条目，`path` 为 `storage:文件名` 或绝对路径，`linkMode` 区分存储/链接 |
| 笔记 | `itemNotes`：列为 `itemID` / `parentItemID` / `note` / `title` |
| 高亮批注 | `itemAnnotations`：`parentItemID` 指向**附件**条目，`text`=高亮原文，`comment`=批注，`pageLabel`=页码 |
| 回收站 | `deletedItems` — 所有查询都应 `NOT IN (SELECT itemID FROM deletedItems)` |

## 容易踩的坑

**日期是双段字符串。** Zotero 7 把 `date` 存成 `"<原始> <规范化>"`，例如 `2020-00-00 2020`、`2026-02-01 2026-02-01`。直接输出会显得重复；排序更不能直接对文本 `ORDER BY`（`"March 2020"` 这类值会排错位）。取年份用规范化段：

```sql
substr(v.value, instr(v.value, ' ') + 1)   -- 只在确定有两段时可靠
```

**附件路径要拼 KEY。** `storage:x.pdf` 对应 `<数据目录>/storage/<附件KEY>/x.pdf`，注意用的是**附件自己的 key**，不是父条目 key。

**链接附件两种前缀。** `attachments:` 表示相对"链接附件根目录"（存在 prefs.js 的 `extensions.zotero.baseAttachmentPath`），纯路径解析会失效；解析不出来就如实报告，不要猜。

**笔记是 HTML。** `itemNotes.note` 是 `text/html` 片段，含 `<div class="zotero-note">` 外壳与 HTML 实体。必须做实体反转义 + 段落换行，单纯正则剥标签会丢掉全部换行、残留 `&amp;` 之类。

**批注挂的是附件，不是文献。** 想按文献取批注，中间必须过一层 `itemAttachments`：

```sql
SELECT an.* FROM itemAnnotations an
JOIN itemAttachments ia ON ia.itemID = an.parentItemID
WHERE ia.parentItemID = :item_id
```

**群体库不是独立的表。** 条目统一存在 `items` 里，靠 `libraryID` 区分（1 = 我的文库）。`groups` 表只存群体库元信息；`groupItems` 是历史遗留空表，不要依赖。

## 字段名对照

程序里用的字段名与界面文案不同，常见几组：

| 界面 | 字段名 |
|---|---|
| 期刊 / 书名 / 会议集 | `publicationTitle` / `bookTitle` / `proceedingsTitle` |
| 日期 | `date` |
| 摘要 | `abstractNote` |
| 卷 / 期 / 页码 | `volume` / `issue` / `pages` |
| 学校 / 学位类型 | `university` / `thesisType` |
| 网址 / 访问日期 | `url` / `accessDate` |
