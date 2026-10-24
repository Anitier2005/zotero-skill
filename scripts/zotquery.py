#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zotquery.py — 本地 Zotero 文献库只读查询工具。

设计约束
--------
1. 永不触碰真实库文件。所有查询都在一次性快照上执行（主库 + -wal / -journal
   同伴文件一起复制到临时目录），因此不会与运行中的 Zotero 抢锁，也不会让
   主库产生任何 journal / wal 残留。
2. 只依赖标准库，Python 3.8+，Windows / macOS / Linux 通用。
3. 所有语句均为 SELECT；快照连接额外设置 PRAGMA query_only = 1 作为第二道防线。

用法
----
    zotquery.py <命令> [选项]

命令
    collections                     列出分类（含层级路径与条目数）
    list                            列出条目
    search <关键词>                 按标题/作者/标签/出版物/摘要检索
    info <KEY>                      单条条目完整详情
    text <KEY>                      读取全文索引缓存（或 HTML 快照正文）
    tags                            列出标签及使用频次
    trash                           查看回收站（--empty 清空）

    locate                          报告 Zotero 程序位置、版本与 API 状态（不启动）
    launch                          启动 Zotero 并等待本地 API 就绪（需 --yes）
    doctor                          检查程序、数据目录与写入通道

    add / update / delete           新建、修改、删除条目（写操作，需 --yes）

全局选项
    --data-dir <目录>               指定 Zotero 数据目录（也可直接给 zotero.sqlite 路径）
    --json                          机器可读输出
    --fresh                         忽略快照缓存，强制重新复制
    --version                       打印版本
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unicodedata
from html.parser import HTMLParser

# 同目录下的本地 API 客户端（写操作走它，绝不直接写 sqlite）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zapi  # noqa: E402
import zlaunch  # noqa: E402

__version__ = "3.1.0"

# 非文献条目类型：这些不是用户在库界面里看到的"一篇文献"
NON_ITEM_TYPES = ("attachment", "note", "annotation")

# 承载"出处"信息的字段（搜索时一并匹配）
PUBLICATION_FIELDS = (
    "publicationTitle", "bookTitle", "proceedingsTitle", "websiteTitle",
    "university", "institution", "publisher",
)
# 深度搜索额外匹配的字段
DEEP_FIELDS = ("abstractNote", "shortTitle", "DOI", "extra", "seriesTitle")


# --------------------------------------------------------------------------
# 输出编码：Windows 控制台默认 GBK，遇到特殊字符会直接崩，统一压成 UTF-8
# --------------------------------------------------------------------------
def _force_utf8_stdout() -> None:
    """统一 UTF-8 + LF：Windows 控制台默认 GBK 会在生僻字上崩，CRLF 也会干扰解析。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")
        except (AttributeError, ValueError):
            pass


# --------------------------------------------------------------------------
# 数据目录定位
# --------------------------------------------------------------------------
def _profile_roots():
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA")
        roots = [os.path.join(base, "Zotero", "Zotero", "Profiles")] if base else []
    elif sys.platform == "darwin":
        roots = [os.path.expanduser("~/Library/Application Support/Zotero/Profiles")]
    else:
        roots = [os.path.expanduser("~/.zotero/zotero")]
    return [r for r in roots if os.path.isdir(r)]


def _data_dirs_from_prefs():
    """从 prefs.js 的 extensions.zotero.dataDir 读出用户自定义的数据目录。"""
    pattern = re.compile(
        r'user_pref\("extensions\.zotero\.dataDir",\s*"((?:[^"\\]|\\.)*)"\)')
    for root in _profile_roots():
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            prefs = os.path.join(root, name, "prefs.js")
            if not os.path.isfile(prefs):
                continue
            try:
                with open(prefs, "r", encoding="utf-8", errors="replace") as fh:
                    m = pattern.search(fh.read())
            except OSError:
                continue
            if m:
                # prefs.js 中 Windows 路径以 \\ 转义
                yield m.group(1).replace("\\\\", "\\")


def locate_data_dir(explicit=None):
    """返回 (数据目录, 来源说明)。找不到时返回 (None, 尝试过的候选列表)。"""
    candidates = []
    if explicit:
        path = os.path.abspath(os.path.expanduser(explicit))
        # 允许直接传 zotero.sqlite 路径
        if os.path.isfile(path):
            path = os.path.dirname(path)
        candidates.append(("--data-dir", path))
    env = os.environ.get("ZOTERO_DATA_DIR")
    if env:
        candidates.append(("ZOTERO_DATA_DIR", os.path.abspath(os.path.expanduser(env))))
    for path in _data_dirs_from_prefs():
        candidates.append(("prefs.js", path))
    candidates.append(("默认位置", os.path.join(os.path.expanduser("~"), "Zotero")))

    tried = []
    for origin, path in candidates:
        if os.path.isfile(os.path.join(path, "zotero.sqlite")):
            return path, origin
        tried.append("{} → {}".format(origin, path))
    return None, tried


# --------------------------------------------------------------------------
# 快照：主库 + -wal/-journal 一起复制，按 mtime/size 复用
# --------------------------------------------------------------------------
def _stamp(paths):
    out = []
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append([os.path.basename(p), st.st_mtime_ns, st.st_size])
    return out


def build_snapshot(data_dir, use_cache=True):
    """把库复制到临时目录并返回快照路径。返回 (快照路径, 缓存目录, 是否新建)。"""
    src = os.path.join(data_dir, "zotero.sqlite")
    if not os.path.isfile(src):
        raise SystemExit("错误：{} 不存在。".format(src))

    sources = [src]
    for suffix in ("-wal", "-journal"):
        if os.path.isfile(src + suffix):
            sources.append(src + suffix)

    key = hashlib.sha1(os.path.abspath(data_dir).encode("utf-8")).hexdigest()[:12]
    cache_dir = os.path.join(tempfile.gettempdir(), "zotquery-cache", key)
    target = os.path.join(cache_dir, "zotero.sqlite")
    stamp_file = os.path.join(cache_dir, "stamp.json")
    stamp = _stamp(sources)

    if use_cache and os.path.isfile(target):
        try:
            with open(stamp_file, "r", encoding="utf-8") as fh:
                if json.load(fh) == stamp:
                    return target, cache_dir, False
        except (OSError, ValueError):
            pass

    os.makedirs(cache_dir, exist_ok=True)
    # 清掉上一轮残留（含我们自己产生的 -wal/-shm），避免污染新快照
    for name in os.listdir(cache_dir):
        try:
            os.unlink(os.path.join(cache_dir, name))
        except OSError:
            pass

    tmp = target + ".part"
    shutil.copyfile(src, tmp)
    for suffix in ("-wal", "-journal"):
        if os.path.isfile(src + suffix):
            shutil.copyfile(src + suffix, tmp + suffix)
    os.replace(tmp, target)
    for suffix in ("-wal", "-journal"):
        part = tmp + suffix
        if os.path.isfile(part):
            os.replace(part, target + suffix)

    try:
        with open(stamp_file, "w", encoding="utf-8") as fh:
            json.dump(stamp, fh)
    except OSError:
        pass
    return target, cache_dir, True


def connect(snapshot_path):
    conn = sqlite3.connect(snapshot_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = 1")  # 第二道防线：任何写操作都会抛异常
    return conn


# --------------------------------------------------------------------------
# 日期归一化（Zotero 7 把日期存成 "<原始> <规范化>"，如 "2020-00-00 2020"）
# --------------------------------------------------------------------------
_DATE_TOKEN = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")
_YEAR_ANYWHERE = re.compile(r"(1[5-9]\d{2}|20\d{2}|21\d{2})")


def display_date(raw):
    if not raw:
        return ""
    for token in reversed(str(raw).split()):
        if _DATE_TOKEN.match(token):
            return token
    return str(raw).strip()


def date_key(raw):
    if not raw:
        return (0, 0, 0)
    for token in reversed(str(raw).split()):
        m = _DATE_TOKEN.match(token)
        if m:
            return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0))
    m = _YEAR_ANYWHERE.search(str(raw))
    return (int(m.group(1)) if m else 0, 0, 0)


# --------------------------------------------------------------------------
# 基础查询
# --------------------------------------------------------------------------
def _field_subquery(field_name):
    return """(
        SELECT v.value FROM itemData d
        JOIN itemDataValues v ON v.valueID = d.valueID
        WHERE d.itemID = i.itemID
          AND d.fieldID = (SELECT fieldID FROM fields WHERE fieldName = '{}')
    )""".format(field_name)


BASE_SQL = """
SELECT i.itemID, i.key, i.libraryID, it.typeName,
       i.dateAdded, i.dateModified,
       {title} AS title,
       {date}  AS date
FROM items i
JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
WHERE i.itemID NOT IN (SELECT itemID FROM deletedItems)
  AND it.typeName NOT IN {non_items}
""".format(
    title=_field_subquery("title"),
    date=_field_subquery("date"),
    non_items=str(NON_ITEM_TYPES).replace('"', "'"),
)


# --------------------------------------------------------------------------
# 区间过滤：支持 2020 / 2020-05 / 2020-05-16 三种粒度
# --------------------------------------------------------------------------
def parse_bound(text, upper=False):
    """把 2020 / 2020-05 / 2020-05-16 解析成可比较的三元组。

    上界（--until）会补齐到该粒度的最后一天，保证「2024」包含整个 2024 年。
    """
    if not text:
        return None
    parts = re.split(r"[-/.]", str(text).strip())
    if not parts or not parts[0].isdigit():
        raise SystemExit("错误：日期「{}」格式无法识别，请用 2020 / 2020-05 / 2020-05-16。".format(text))
    year = int(parts[0])
    month = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else (12 if upper else 0)
    day = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else (31 if upper else 0)
    return (year, month, day)


def in_range(value, lo, hi):
    if lo is None and hi is None:
        return True
    stamp = date_key(value)
    if stamp == (0, 0, 0):
        return False
    if lo is not None and stamp < lo:
        return False
    if hi is not None and stamp > hi:
        return False
    return True



def _rows_to_dicts(rows):
    return [dict(r) for r in rows]


def _public(rows):
    """输出前剔除内部字段（下划线开头的排序辅助字段）。"""
    return [
        {k: v for k, v in r.items() if not k.startswith("_")}
        for r in rows
    ]


def _normalize_dates(rows):
    """排完序后再把 Zotero 7 的 "原始 规范化" 双段日期收敛成可读形式。"""
    for row in rows:
        row["date"] = display_date(row.get("date"))
    return rows


def fetch_items(conn, extra_where="", params=(), sort_desc=True):
    sql = "SELECT * FROM ({}) AS t".format(BASE_SQL)
    if extra_where:
        sql += " WHERE " + extra_where
    rows = _rows_to_dicts(conn.execute(sql, params).fetchall())
    rows.sort(key=lambda r: (date_key(r["date"]), r["itemID"]), reverse=sort_desc)
    return _normalize_dates(rows)


def creators_map(conn, item_ids):
    """一次性取回多条目的作者，避免逐行 N+1 查询。"""
    if not item_ids:
        return {}
    marks = ",".join("?" * len(item_ids))
    rows = conn.execute(
        """SELECT ic.itemID, cr.firstName, cr.lastName, ct.creatorType, ic.orderIndex
           FROM itemCreators ic
           JOIN creators cr ON ic.creatorID = cr.creatorID
           JOIN creatorTypes ct ON ic.creatorTypeID = ct.creatorTypeID
           WHERE ic.itemID IN ({})
           ORDER BY ic.itemID, ic.orderIndex""".format(marks),
        list(item_ids),
    ).fetchall()
    out = {}
    for r in rows:
        name = " ".join(p for p in (r["firstName"], r["lastName"]) if p)
        out.setdefault(r["itemID"], []).append(
            {"name": name.strip(), "role": r["creatorType"]}
        )
    return out


def describe(rows, conn):
    """把条目行渲染成 "[KEY] (年) 类型 标题 — 作者" """
    cmap = creators_map(conn, [r["itemID"] for r in rows])
    lines = []
    for r in rows:
        year = display_date(r["date"])[:4] or "----"
        line = "[{}] ({}) {} {}".format(
            r["key"], year, r["typeName"], r["title"] or "(无标题)"
        )
        authors = cmap.get(r["itemID"], [])
        if authors:
            names = ", ".join(a["name"] for a in authors[:3])
            if len(authors) > 3:
                names += " 等"
            line += " — " + names
        lines.append(line)
    return lines


# --------------------------------------------------------------------------
# 分类（collection）树
# --------------------------------------------------------------------------
def collection_table(conn):
    rows = conn.execute(
        """SELECT c.collectionID, c.key, c.collectionName, c.parentCollectionID, c.libraryID,
                  (SELECT COUNT(*) FROM collectionItems ci
                    WHERE ci.collectionID = c.collectionID) AS n
           FROM collections c"""
    ).fetchall()
    info = {}
    for r in rows:
        info[r["collectionID"]] = {
            "id": r["collectionID"],
            "key": r["key"],
            "name": r["collectionName"],
            "parent": r["parentCollectionID"],
            "libraryID": r["libraryID"],
            "count": r["n"],
        }
    # 计算层级路径，用于消除重名歧义
    for cid, node in info.items():
        parts, cur, guard = [], node, 0
        while cur is not None and guard < 32:
            parts.append(cur["name"])
            cur = info.get(cur["parent"])
            guard += 1
        node["path"] = " / ".join(reversed(parts))
        node["depth"] = len(parts) - 1
    return info


def resolve_collection(info, query):
    """按完整路径 → 名称 → 子串的顺序解析分类，返回 collectionID 集合（含子孙）。"""
    q = query.strip()
    folded = q.casefold()
    nodes = list(info.values())

    hit = [n for n in nodes if n["path"].casefold() == folded]
    if not hit:
        hit = [n for n in nodes if n["name"].casefold() == folded]
    if not hit:
        hit = [n for n in nodes if folded in n["path"].casefold()]
    if not hit:
        raise SystemExit("错误：找不到分类「{}」。用 collections 命令查看全部分类。".format(q))
    if len(hit) > 1:
        listing = "\n".join("  - " + n["path"] for n in hit[:10])
        raise SystemExit(
            "错误：分类「{}」不唯一，请用完整路径指定：\n{}".format(q, listing)
        )

    root = hit[0]["id"]
    ids, stack = set(), [root]
    while stack:
        cur = stack.pop()
        ids.add(cur)
        stack.extend(n["id"] for n in nodes if n["parent"] == cur)
    return ids, hit[0]


# --------------------------------------------------------------------------
# 检索
# --------------------------------------------------------------------------
def escape_like(text):
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --------------------------------------------------------------------------
# 详情
# --------------------------------------------------------------------------
class _HTMLText(HTMLParser):
    _BLOCK = {
        "p", "div", "li", "ul", "ol", "tr", "td", "th", "table", "blockquote",
        "pre", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
            return
        if tag == "br":
            self.parts.append("\n")
            return
        if tag == "li":
            self.parts.append("\n- ")
            return
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            if self._skip:
                self._skip -= 1
            return
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self):
        raw = "".join(self.parts)
        raw = raw.replace("\u00a0", " ")
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r" *\n *", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def html_to_text(markup):
    """Zotero 笔记是富文本 HTML，必须解实体、保留段落结构，不能只剥标签。"""
    if not markup:
        return ""
    parser = _HTMLText()
    try:
        parser.feed(markup)
        parser.close()
        return parser.text()
    except Exception:
        return re.sub(r"<[^>]+>", " ", markup).strip()


def item_fields(conn, item_id):
    rows = conn.execute(
        """SELECT f.fieldName, v.value FROM itemData d
           JOIN fields f ON d.fieldID = f.fieldID
           JOIN itemDataValues v ON d.valueID = v.valueID
           WHERE d.itemID = ?""",
        (item_id,),
    ).fetchall()
    return {r["fieldName"]: r["value"] for r in rows}


def resolve_key(conn, key):
    """key 可能是附件/笔记/批注，自动回溯到它所属的文献条目。"""
    row = conn.execute(
        """SELECT i.itemID, i.key, it.typeName FROM items i
           JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
           WHERE i.key = ?""",
        (key,),
    ).fetchone()
    if not row:
        return None, "找不到 key = {} 的条目。".format(key)

    if row["typeName"] not in NON_ITEM_TYPES:
        return row, None

    parent = None
    if row["typeName"] == "attachment":
        parent = conn.execute(
            "SELECT parentItemID FROM itemAttachments WHERE itemID = ?", (row["itemID"],)
        ).fetchone()
    elif row["typeName"] == "note":
        parent = conn.execute(
            "SELECT parentItemID FROM itemNotes WHERE itemID = ?", (row["itemID"],)
        ).fetchone()
    else:
        parent = conn.execute(
            """SELECT ia.parentItemID FROM itemAnnotations an
               JOIN itemAttachments ia ON ia.itemID = an.parentItemID
               WHERE an.itemID = ?""",
            (row["itemID"],),
        ).fetchone()

    if not parent or not parent["parentItemID"]:
        return None, "{} 是独立的 {}（没有父条目），请直接读取其文件或内容。".format(
            key, row["typeName"]
        )
    return resolve_key(
        conn,
        conn.execute(
            "SELECT key FROM items WHERE itemID = ?", (parent["parentItemID"],)
        ).fetchone()["key"],
    )


def attachment_path(data_dir, att_key, raw_path):
    if not raw_path:
        return None
    if raw_path.startswith("storage:"):
        return os.path.join(data_dir, "storage", att_key, raw_path[len("storage:"):])
    if raw_path.startswith("attachments:"):
        # 链接附件，相对「链接附件根目录」；根目录存在 prefs.js 里，此处只做标记
        return raw_path
    return raw_path


def item_attachments(conn, data_dir, item_id):
    rows = conn.execute(
        """SELECT i.key, ia.path, ia.contentType, ia.linkMode
           FROM itemAttachments ia JOIN items i ON ia.itemID = i.itemID
           WHERE ia.parentItemID = ?
             AND ia.itemID NOT IN (SELECT itemID FROM deletedItems)
           ORDER BY ia.itemID""",
        (item_id,),
    ).fetchall()
    out = []
    for r in rows:
        path = attachment_path(data_dir, r["key"], r["path"])
        exists = bool(path and not path.startswith("attachments:") and os.path.isfile(path))
        size = os.path.getsize(path) if exists else None
        cache = None
        if exists:
            candidate = os.path.join(os.path.dirname(path), ".zotero-ft-cache")
            if os.path.isfile(candidate):
                cache = candidate
        out.append(
            {
                "key": r["key"],
                "contentType": r["contentType"],
                "linkMode": r["linkMode"],
                "path": path,
                "exists": exists,
                "size": size,
                "ftCache": cache,
                "linkedRoot": bool(r["path"] and str(r["path"]).startswith("attachments:")),
            }
        )
    return out


def item_notes(conn, item_id):
    rows = conn.execute(
        """SELECT n.note, n.title FROM itemNotes n
           WHERE n.parentItemID = ?
             AND n.itemID NOT IN (SELECT itemID FROM deletedItems)
           ORDER BY n.itemID""",
        (item_id,),
    ).fetchall()
    return [
        {"title": r["title"] or "", "text": html_to_text(r["note"])}
        for r in rows
        if r["note"]
    ]


def item_annotations(conn, item_id):
    rows = conn.execute(
        """SELECT an.type, an.text, an.comment, an.pageLabel, an.color
           FROM itemAnnotations an
           JOIN itemAttachments ia ON ia.itemID = an.parentItemID
           WHERE ia.parentItemID = ?
             AND an.itemID NOT IN (SELECT itemID FROM deletedItems)
           ORDER BY an.sortIndex, an.itemID""",
        (item_id,),
    ).fetchall()
    return [
        {
            "type": r["type"],
            "text": r["text"] or "",
            "comment": r["comment"] or "",
            "page": r["pageLabel"] or "",
            "color": r["color"] or "",
        }
        for r in rows
    ]


def item_info(conn, data_dir, key):
    row, err = resolve_key(conn, key)
    if err:
        raise SystemExit("错误：" + err)
    coll = collection_table(conn)
    item_colls = conn.execute(
        "SELECT collectionID FROM collectionItems WHERE itemID = ?", (row["itemID"],)
    ).fetchall()
    return {
        "key": row["key"],
        "itemType": row["typeName"],
        "fields": item_fields(conn, row["itemID"]),
        "creators": creators_map(conn, [row["itemID"]]).get(row["itemID"], []),
        "tags": [
            r["name"]
            for r in conn.execute(
                """SELECT t.name FROM itemTags ig JOIN tags t ON ig.tagID = t.tagID
                   WHERE ig.itemID = ? ORDER BY t.name""",
                (row["itemID"],),
            ).fetchall()
        ],
        "collections": [
            coll[r["collectionID"]]["path"]
            for r in item_colls
            if r["collectionID"] in coll
        ],
        "attachments": item_attachments(conn, data_dir, row["itemID"]),
        "notes": item_notes(conn, row["itemID"]),
        "annotations": item_annotations(conn, row["itemID"]),
    }


def pick_attachment(attachments, prefer_key=None):
    """挑最合适的全文来源：指定 key > PDF > 有索引缓存 > 体积最大。"""
    usable = [a for a in attachments if a["exists"]]
    if prefer_key:
        for a in usable:
            if a["key"] == prefer_key:
                return a
        return None
    if not usable:
        return None

    def rank(a):
        is_pdf = (a["contentType"] or "").lower() == "application/pdf"
        return (1 if is_pdf else 0, 1 if a["ftCache"] else 0, a["size"] or 0)

    return max(usable, key=rank)


def read_fulltext(att):
    """优先 .zotero-ft-cache；HTML 快照退化为解析正文。返回 (文本, 来源说明)。"""
    if att["ftCache"]:
        with open(att["ftCache"], "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(), "全文索引缓存"
    path = att["path"] or ""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".html", ".htm", ".xhtml"):
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return html_to_text(fh.read()), "HTML 快照正文"
    return None, None


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
INFO_FIELD_ORDER = (
    "title", "shortTitle", "publicationTitle", "bookTitle", "proceedingsTitle",
    "websiteTitle", "conferenceName", "date", "volume", "issue", "pages",
    "edition", "publisher", "place", "university", "thesisType", "institution",
    "reportType", "reportNumber", "seriesTitle", "DOI", "ISBN", "ISSN", "url",
    "language", "abstractNote", "extra",
)
FIELD_LABELS = {
    "title": "标题", "shortTitle": "短标题", "publicationTitle": "期刊",
    "bookTitle": "书名", "proceedingsTitle": "会议集", "websiteTitle": "网站",
    "conferenceName": "会议", "date": "日期", "volume": "卷", "issue": "期",
    "pages": "页码", "edition": "版次", "publisher": "出版方", "place": "地点",
    "university": "学校", "thesisType": "学位类型", "institution": "机构",
    "reportType": "报告类型", "reportNumber": "报告编号", "seriesTitle": "丛编",
    "DOI": "DOI", "ISBN": "ISBN", "ISSN": "ISSN", "url": "URL", "language": "语言",
    "abstractNote": "摘要", "extra": "附加", "libraryCatalog": "馆藏目录",
    "callNumber": "索书号", "rights": "版权", "accessDate": "访问日期",
}


def clip(text, limit, hint):
    if limit and len(text) > limit:
        return "{}\n…[已截断：原文 {} 字，本次输出前 {} 字。{}]".format(
            text[:limit], len(text), limit, hint
        )
    return text


def _width(text):
    """终端显示宽度：CJK 全角按 2 列算，保证中英文混排能对齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _labeled(label, value, width=10):
    return "{}:{}{}".format(label, " " * max(1, width - _width(label) - 1), value)


def render_info(info, max_note=4000, max_annotation=600):
    f = info["fields"]
    out = ["=" * 64, "[{}] {}".format(info["key"], info["itemType"])]
    if f.get("title"):
        out.append(_labeled("标题", f["title"]))

    printed = {"title", "abstractNote"}
    if info["creators"]:
        names = "; ".join(
            "{}{}".format(
                c["name"], "" if c["role"] == "author" else "（{}）".format(c["role"])
            )
            for c in info["creators"]
        )
        out.append(_labeled("作者", names))

    for field in INFO_FIELD_ORDER:
        if field == "title" or field not in f or not f[field]:
            continue
        if field == "abstractNote":
            continue
        label = FIELD_LABELS.get(field, field)
        value = display_date(f[field]) if field == "date" else f[field]
        out.append(_labeled(label, value))
        printed.add(field)

    # 不在固定顺序里的字段也要输出，否则会被静默丢弃
    for field in sorted(f):
        if field in printed or not f[field]:
            continue
        out.append(_labeled(FIELD_LABELS.get(field, field), f[field]))
        printed.add(field)

    if f.get("abstractNote"):
        out.append("\n摘要:\n" + str(f["abstractNote"]).strip())
    if info["tags"]:
        out.append("\n" + _labeled("标签", ", ".join(info["tags"])))
    if info["collections"]:
        out.append(_labeled("分类", " | ".join(info["collections"])))

    if info["attachments"]:
        out.append("\n附件:")
        for a in info["attachments"]:
            if a["linkedRoot"]:
                state = "链接附件（根目录未知，需在 Zotero 内确认）"
            elif a["exists"]:
                state = "{} KB".format(max(1, (a["size"] or 0) // 1024))
            else:
                state = "文件缺失"
            out.append("  [{}] {} {}".format(a["key"], state, a["contentType"] or ""))
            out.append("       " + str(a["path"]))
            if a["ftCache"]:
                out.append("       全文缓存: 有")

    if info["notes"]:
        out.append("\n笔记（{} 条）:".format(len(info["notes"])))
        for n in info["notes"]:
            body = clip(n["text"], max_note, "改 --max-note 0 看全文")
            out.append("  - " + body.replace("\n", "\n    "))
    if info["annotations"]:
        out.append("\n批注/高亮（{} 条）:".format(len(info["annotations"])))
        for a in info["annotations"]:
            quote = "「{}」".format(clip(a["text"], max_annotation, "")) if a["text"] else "（{}）".format(a["type"])
            line = "  - p{} {}".format(a["page"] or "?", quote)
            if a["comment"]:
                line += " ｜ 批注: " + a["comment"]
            out.append(line)
    return "\n".join(out)


# --------------------------------------------------------------------------
# 检索：命中域
# --------------------------------------------------------------------------
SCOPE_FIELDS = {
    "publication": PUBLICATION_FIELDS,
    "abstract": ("abstractNote",),
    "doi": ("DOI",),
    "callnumber": ("callNumber",),
}

SCOPE_ALIASES = {
    "title": "title", "标题": "title",
    "creator": "creator", "author": "creator", "作者": "creator",
    "tag": "tag", "标签": "tag",
    "publication": "publication", "期刊": "publication", "出处": "publication",
    "abstract": "abstract", "摘要": "abstract",
    "doi": "doi",
    "callnumber": "callnumber", "索书号": "callnumber",
    "notes": "notes", "note": "notes", "笔记": "notes",
    "annotations": "annotations", "annotation": "annotations", "批注": "annotations",
    "fulltext": "fulltext", "全文": "fulltext",
}

DEFAULT_SCOPES = ("title", "creator", "tag", "publication")
DEEP_SCOPES = DEFAULT_SCOPES + ("abstract", "doi", "callnumber")
EVERYTHING_SCOPES = DEEP_SCOPES + ("notes", "annotations", "fulltext")


def normalize_scopes(raw, deep=False, everything=False):
    if everything:
        return list(EVERYTHING_SCOPES)
    if not raw:
        return list(DEEP_SCOPES if deep else DEFAULT_SCOPES)
    out = []
    for item in raw:
        for token in str(item).replace(",", " ").split():
            key = SCOPE_ALIASES.get(token.strip().lower()) or SCOPE_ALIASES.get(token.strip())
            if not key:
                raise SystemExit(
                    "错误：未知检索域「{}」。可选：{}".format(
                        token, " / ".join(sorted(set(SCOPE_ALIASES.values())))
                    )
                )
            if key not in out:
                out.append(key)
    return out


def _scope_sql(scope):
    """返回 (SQL 片段, 需要的 ? 占位符个数)。片段只引用别名 t。"""
    if scope == "title":
        return "LOWER(COALESCE(t.title, '')) LIKE LOWER(?) ESCAPE '\\'", 1
    if scope == "creator":
        return (
            """EXISTS (SELECT 1 FROM itemCreators ic
                        JOIN creators cr ON ic.creatorID = cr.creatorID
                       WHERE ic.itemID = t.itemID
                         AND (LOWER(COALESCE(cr.lastName, ''))  LIKE LOWER(?) ESCAPE '\\'
                           OR LOWER(COALESCE(cr.firstName, '')) LIKE LOWER(?) ESCAPE '\\'))""",
            2,
        )
    if scope == "tag":
        return (
            """EXISTS (SELECT 1 FROM itemTags ig JOIN tags g ON ig.tagID = g.tagID
                       WHERE ig.itemID = t.itemID
                         AND LOWER(g.name) LIKE LOWER(?) ESCAPE '\\')""",
            1,
        )
    if scope == "notes":
        return (
            """EXISTS (SELECT 1 FROM itemNotes n
                       WHERE n.parentItemID = t.itemID
                         AND n.itemID NOT IN (SELECT itemID FROM deletedItems)
                         AND LOWER(n.note) LIKE LOWER(?) ESCAPE '\\')""",
            1,
        )
    if scope == "annotations":
        return (
            """EXISTS (SELECT 1 FROM itemAnnotations an
                        JOIN itemAttachments ia ON ia.itemID = an.parentItemID
                       WHERE ia.parentItemID = t.itemID
                         AND an.itemID NOT IN (SELECT itemID FROM deletedItems)
                         AND LOWER(COALESCE(an.text, '') || ' ' || COALESCE(an.comment, ''))
                             LIKE LOWER(?) ESCAPE '\\')""",
            1,
        )
    fields = SCOPE_FIELDS.get(scope, ())
    if not fields:
        raise SystemExit("错误：检索域「{}」没有对应字段。".format(scope))
    marks = ",".join("?" * len(fields))
    return (
        """EXISTS (SELECT 1 FROM itemData d
                    JOIN fields f ON d.fieldID = f.fieldID
                    JOIN itemDataValues v ON d.valueID = v.valueID
                   WHERE d.itemID = t.itemID
                     AND f.fieldName IN ({})
                     AND LOWER(v.value) LIKE LOWER(?) ESCAPE '\\')""".format(marks),
        len(fields) + 1,
    )


def fulltext_item_keys(conn, data_dir, query):
    """扫描 storage/*/.zotero-ft-cache，返回命中的文献条目 KEY 集合。

    只在显式指定 --in fulltext 时调用：要读磁盘上全部索引文件，库大时会慢。
    """
    needle = query.casefold()
    storage = os.path.join(data_dir, "storage")
    if not os.path.isdir(storage):
        return set()

    attachment_keys = set()
    for name in os.listdir(storage):
        cache = os.path.join(storage, name, ".zotero-ft-cache")
        if not os.path.isfile(cache):
            continue
        try:
            with open(cache, "r", encoding="utf-8", errors="ignore") as fh:
                if needle in fh.read().casefold():
                    attachment_keys.add(name)
        except OSError:
            continue

    if not attachment_keys:
        return set()

    rows = conn.execute(
        """SELECT i.key AS att_key, p.key AS parent_key
           FROM itemAttachments ia
           JOIN items i ON ia.itemID = i.itemID
           JOIN items p ON ia.parentItemID = p.itemID"""
    ).fetchall()
    return {r["parent_key"] for r in rows if r["att_key"] in attachment_keys}


# --------------------------------------------------------------------------
# 检索：过滤 / 排序 / 分页
# --------------------------------------------------------------------------
def _iso_day(value):
    """dateAdded / dateModified 是 "2020-05-16 12:00:00"，取日期部分即可比较。"""
    return str(value or "")[:10]


def scope_filter_ids(conn, collection=None, tag=None):
    """把 --collection / --tag 解析成 itemID 集合；都没指定时返回 None。"""
    ids = None
    if collection:
        picked, _ = resolve_collection(collection_table(conn), collection)
        ids = {
            r["itemID"]
            for r in conn.execute("SELECT itemID, collectionID FROM collectionItems")
            if r["collectionID"] in picked
        }
    if tag:
        tagged = {
            r["itemID"]
            for r in conn.execute(
                """SELECT ig.itemID FROM itemTags ig JOIN tags t ON ig.tagID = t.tagID
                   WHERE LOWER(t.name) = LOWER(?)""",
                (tag,),
            )
        }
        ids = tagged if ids is None else (ids & tagged)
    return ids


def apply_filters(conn, rows, args):
    if getattr(args, "type", None):
        rows = [r for r in rows if r["typeName"] == args.type]

    ids = scope_filter_ids(conn, getattr(args, "collection", None), getattr(args, "tag", None))
    if ids is not None:
        rows = [r for r in rows if r["itemID"] in ids]

    lo, hi = parse_bound(getattr(args, "since", None)), parse_bound(
        getattr(args, "until", None), upper=True
    )
    if lo or hi:
        rows = [r for r in rows if in_range(r["date"], lo, hi)]

    for attr, column in (("added", "dateAdded"), ("modified", "dateModified")):
        raw = getattr(args, attr, None)
        if not raw:
            continue
        if "-" in str(raw) and ".." in str(raw):
            start, end = str(raw).split("..", 1)
        else:
            start = end = str(raw)
        rows = [r for r in rows if start <= _iso_day(r[column]) <= end]

    if getattr(args, "untagged", False):
        tagged = {r["itemID"] for r in conn.execute("SELECT DISTINCT itemID FROM itemTags")}
        rows = [r for r in rows if r["itemID"] not in tagged]

    if getattr(args, "missing", None):
        rows = missing_filter(conn, rows, args.missing)

    return rows


def missing_filter(conn, rows, kind):
    kind = str(kind).strip().lower()
    field_map = {"doi": "DOI", "abstract": "abstractNote", "date": "date", "title": "title"}
    if kind in field_map:
        have = {
            r["itemID"]
            for r in conn.execute(
                """SELECT d.itemID FROM itemData d
                   JOIN fields f ON d.fieldID = f.fieldID
                   JOIN itemDataValues v ON d.valueID = v.valueID
                   WHERE f.fieldName = ? AND TRIM(v.value) <> ''""",
                (field_map[kind],),
            )
        }
        return [r for r in rows if r["itemID"] not in have]
    if kind == "tag":
        have = {r["itemID"] for r in conn.execute("SELECT DISTINCT itemID FROM itemTags")}
        return [r for r in rows if r["itemID"] not in have]
    if kind == "collection":
        have = {r["itemID"] for r in conn.execute("SELECT DISTINCT itemID FROM collectionItems")}
        return [r for r in rows if r["itemID"] not in have]
    if kind in ("pdf", "attachment"):
        have = {
            r["parentItemID"]
            for r in conn.execute(
                """SELECT ia.parentItemID FROM itemAttachments ia
                   WHERE ia.contentType = 'application/pdf'
                     AND ia.itemID NOT IN (SELECT itemID FROM deletedItems)"""
            )
        }
        return [r for r in rows if r["itemID"] not in have]
    raise SystemExit(
        "错误：--missing 只支持 doi / abstract / date / title / tag / collection / pdf。"
    )


SORT_FIELDS = ("date", "added", "modified", "title", "creator", "type")


def sort_rows(conn, rows, field="date", order="desc"):
    reverse = str(order).lower() != "asc"
    if field == "date":
        rows.sort(key=lambda r: (date_key(r["date"]), r["itemID"]), reverse=reverse)
    elif field == "added":
        rows.sort(key=lambda r: (_iso_day(r["dateAdded"]), r["itemID"]), reverse=reverse)
    elif field == "modified":
        rows.sort(key=lambda r: (_iso_day(r["dateModified"]), r["itemID"]), reverse=reverse)
    elif field == "title":
        rows.sort(key=lambda r: (r["title"] or "").casefold(), reverse=reverse)
    elif field == "type":
        rows.sort(key=lambda r: (r["typeName"] or "", date_key(r["date"])), reverse=reverse)
    elif field == "creator":
        cmap = creators_map(conn, [r["itemID"] for r in rows])
        rows.sort(
            key=lambda r: (
                (cmap.get(r["itemID"]) or [{"name": "\uffff"}])[0]["name"].casefold(),
                date_key(r["date"]),
            ),
            reverse=reverse,
        )
    else:
        raise SystemExit("错误：--sort 只支持 " + " / ".join(SORT_FIELDS))
    return rows


def search_items(conn, data_dir, query, scopes, exact=False):
    """按指定域检索，返回条目行（未过滤、未排序）。"""
    if exact:
        pattern = escape_like(query)
    else:
        pattern = "%" + escape_like(query) + "%"

    sql_scopes = [s for s in scopes if s != "fulltext"]
    if sql_scopes:
        conds, params = [], []
        for scope in sql_scopes:
            piece, arity = _scope_sql(scope)
            conds.append("(" + piece + ")")
            params.extend([pattern] * arity)
        sql = "SELECT * FROM ({}) AS t WHERE {}".format(BASE_SQL, " OR ".join(conds))
        rows = _rows_to_dicts(conn.execute(sql, params).fetchall())
    else:
        rows = fetch_items(conn)

    if "fulltext" in scopes:
        allowed = fulltext_item_keys(conn, data_dir, query)
        rows = [r for r in rows if r["key"] in allowed]
    return rows


# --------------------------------------------------------------------------
# 写操作：公共设施
# --------------------------------------------------------------------------
def api_client(base=None):
    return zapi.LocalApi(base=base or os.environ.get("ZOTERO_LOCAL_API") or zapi.DEFAULT_BASE)


def write_gate(args, lines, summary):
    """变更闸门：默认只打印预览并返回 False；--yes 才放行。"""
    print("\n".join(lines))
    if args.dry_run:
        print("\n—— 预览模式（--dry-run），未发送任何请求。")
        return False
    if args.yes:
        return True
    print("\n—— 未执行，以上为变更预览。")
    print("   请把上述内容交给用户确认，得到明确同意后再加 --yes 重新运行。")
    return False


def handle_api_error(exc):
    message = [str(exc)]
    if getattr(exc, "hint", None):
        message.append("提示：" + exc.hint)
    print("错误：" + "\n".join(message), file=sys.stderr)
    return 1


def _print_key_hint(api):
    """提醒用户选「始终允许」，否则每次写入都要重新授权。"""
    if getattr(api, "_remember", False):
        return
    print("提示：本次授权为一次性。若不想每次写入都弹窗，下次授权时选「始终允许」。")


FIELD_ALIAS = {label: name for name, label in FIELD_LABELS.items()}
KNOWN_FIELDS = set(INFO_FIELD_ORDER) | {
    "abstractNote", "extra", "language", "libraryCatalog", "callNumber",
    "rights", "accessDate", "series", "seriesTitle", "journalAbbreviation",
    "conferenceName", "publisher", "place", "edition", "numPages",
}


def resolve_field_name(raw):
    name = str(raw).strip()
    if name in KNOWN_FIELDS:
        return name, True
    if name in FIELD_ALIAS:
        return FIELD_ALIAS[name], True
    lowered = {f.lower(): f for f in KNOWN_FIELDS}
    if name.lower() in lowered:
        return lowered[name.lower()], True
    return name, False


def split_assignment(raw):
    if "=" not in str(raw):
        raise SystemExit("错误：--set 需要 FIELD=VALUE 形式，收到「{}」。".format(raw))
    name, value = str(raw).split("=", 1)
    return name.strip(), value


def parse_creator(raw):
    text = str(raw).strip()
    role = "author"
    if "#" in text:
        text, role = text.rsplit("#", 1)
        text, role = text.strip(), (role.strip() or "author")
    if not text:
        raise SystemExit("错误：--creator 不能为空。")
    if "," in text:
        last, first = [p.strip() for p in text.split(",", 1)]
        return {"creatorType": role, "lastName": last, "firstName": first}
    return {"creatorType": role, "lastName": text, "firstName": ""}


def resolve_collection_keys(conn, names):
    table = collection_table(conn)
    picked_nodes = []
    for name in names:
        _ids, node = resolve_collection(table, name)
        picked_nodes.append(node)
    return picked_nodes


def describe_row(row, conn, cmap=None):
    year = display_date(row.get("date"))[:4] or "----"
    line = "[{}] ({}) {} {}".format(
        row.get("key"), year, row.get("typeName"), row.get("title") or "(无标题)"
    )
    authors = (cmap or creators_map(conn, [row["itemID"]])).get(row["itemID"], [])
    if authors:
        names = ", ".join(a["name"] for a in authors[:3])
        line += " — " + names
    return line


# --------------------------------------------------------------------------
# 写操作：新增
# --------------------------------------------------------------------------
def build_new_items(args, conn):
    items = []
    if args.from_json:
        source = sys.stdin.read() if args.from_json == "-" else open(
            args.from_json, "r", encoding="utf-8"
        ).read()
        data = json.loads(source)
        return data if isinstance(data, list) else [data]

    item_type = args.type or "journalArticle"
    if item_type == "note":
        body = (args.text or "").strip()
        if not body:
            raise SystemExit("错误：新增笔记需要 --text。")
        paragraphs = "".join(
            "<p>{}</p>".format(_escape(p)) for p in body.split("\n\n") if p.strip()
        )
        item = {
            "itemType": "note",
            "note": '<div data-schema-version="9">{}</div>'.format(paragraphs),
            "tags": [{"tag": t} for t in (args.tag or [])],
        }
        if args.parent:
            item["parentItem"] = args.parent
        return [item]

    item = {"itemType": item_type, "creators": [], "tags": []}
    if args.title:
        item["title"] = args.title
    for raw in args.set or []:
        name, value = split_assignment(raw)
        field, known = resolve_field_name(name)
        if not known:
            print("警告：字段「{}」不在常见字段表内，Zotero 可能拒绝。".format(name), file=sys.stderr)
        item[field] = value
    if not item.get("title"):
        raise SystemExit("错误：新增文献至少需要 --title。")
    for raw in args.creator or []:
        item["creators"].append(parse_creator(raw))
    item["tags"] = [{"tag": t} for t in (args.tag or [])]
    collections = resolve_collection_keys(conn, args.collection or [])
    if collections:
        item["collections"] = [n["key"] for n in collections]
    if args.parent:
        item["parentItem"] = args.parent
    return [item]


def _escape(text):
    import html as _html

    return _html.escape(text, quote=False)


def render_add_preview(items, conn):
    lines = ["将新增 {} 条条目：".format(len(items))]
    for item in items:
        lines.append("")
        lines.append("  类型: {}".format(item.get("itemType")))
        title = item.get("title") or _strip_html(item.get("note", ""))[:40]
        lines.append("  标题: {}".format(title))
        creators = item.get("creators") or []
        if creators:
            lines.append("  作者: " + "; ".join(
                "{} {}".format(c.get("lastName", ""), c.get("firstName", "")).strip()
                for c in creators
            ))
        for field in ("date", "publicationTitle", "DOI", "url"):
            if item.get(field):
                lines.append("  {}: {}".format(FIELD_LABELS.get(field, field), item[field]))
        tags = [t.get("tag") for t in item.get("tags") or [] if t.get("tag")]
        if tags:
            lines.append("  标签: " + ", ".join(tags))
        colls = item.get("collections") or []
        if colls:
            names = _collection_names(conn, colls)
            lines.append("  分类: " + ", ".join(names))
        if item.get("parentItem"):
            lines.append("  父条目: " + item["parentItem"])
    return lines


def _strip_html(markup):
    return html_to_text(markup or "")


def _collection_names(conn, keys):
    mapping = {
        r["key"]: r["collectionName"]
        for r in conn.execute("SELECT key, collectionName FROM collections")
    }
    return [mapping.get(k, k) for k in keys]


def cmd_add(args, conn, data_dir):
    items = build_new_items(args, conn)
    lines = render_add_preview(items, conn)
    if not write_gate(args, lines, "新增"):
        return 0 if args.dry_run else 3

    api = api_client(opts_base(args))
    try:
        successful, failed = api.create_items(items)
    except zapi.ApiError as exc:
        return handle_api_error(exc)

    if failed:
        print("部分条目创建失败：", file=sys.stderr)
        for key, detail in failed.items():
            print("  {}: {}".format(key, detail), file=sys.stderr)
    created = list(successful.values()) if isinstance(successful, dict) else []
    if args.json:
        print(json.dumps({"successful": successful, "failed": failed},
                         ensure_ascii=False, indent=2))
    else:
        print("已新增 {} 条：".format(len(created)))
        for entry in created:
            data = entry.get("data", entry) if isinstance(entry, dict) else {}
            print("  [{}] {}".format(entry.get("key", "?"), data.get("title", "")))
    return 0 if not failed else 1


# --------------------------------------------------------------------------
# 写操作：修改
# --------------------------------------------------------------------------
def collect_updates(args, conn, current):
    """把命令行参数翻译成结构化变更：字段、标签、分类（都含显示名与 API key）。"""
    field_changes = []
    for raw in args.set or []:
        name, value = split_assignment(raw)
        field, known = resolve_field_name(name)
        if not known:
            print("警告：字段「{}」不在常见字段表内。".format(name), file=sys.stderr)
        field_changes.append((field, _show_value(field, current["fields"].get(field)), value))
    for raw in args.unset or []:
        field, _ = resolve_field_name(raw)
        field_changes.append((field, _show_value(field, current["fields"].get(field)), None))

    existing_tags = list(current["tags"])
    add_tags, drop_tags = list(args.add_tag or []), list(args.remove_tag or [])
    for tag in add_tags:
        if tag in existing_tags:
            raise SystemExit("错误：标签「{}」已存在。".format(tag))
    for tag in drop_tags:
        if tag not in existing_tags:
            raise SystemExit("错误：标签「{}」不存在，无法删除。".format(tag))

    add_nodes = resolve_collection_keys(conn, args.add_to_collection or [])
    drop_nodes = resolve_collection_keys(conn, args.remove_from_collection or [])
    existing_coll_keys = {
        r["key"]
        for r in conn.execute(
            """SELECT c.key FROM collectionItems ci
               JOIN collections c ON ci.collectionID = c.collectionID
               WHERE ci.itemID = ?""",
            (current["itemID"],),
        )
    }
    for node in drop_nodes:
        if node["key"] not in existing_coll_keys:
            raise SystemExit(
                "错误：该条目不在分类「{}」中，无法移除。".format(node["name"])
            )
    for node in add_nodes:
        if node["key"] in existing_coll_keys:
            raise SystemExit("错误：该条目已在分类「{}」中。".format(node["name"]))

    return {
        "fields": field_changes,
        "add_tags": add_tags,
        "drop_tags": drop_tags,
        "add_colls": [n["key"] for n in add_nodes],
        "drop_colls": [n["key"] for n in drop_nodes],
        "add_coll_names": [n["name"] for n in add_nodes],
        "drop_coll_names": [n["name"] for n in drop_nodes],
    }


def render_update_preview(key, current, changes):
    lines = ["将修改 [{}] {}".format(key, current["fields"].get("title", "")), ""]
    any_change = False
    for field, before, after in changes["fields"]:
        any_change = True
        lines.append("  {}: {}  →  {}".format(
            FIELD_LABELS.get(field, field), _brief(before), _brief(after)))
    for tag in changes["add_tags"]:
        any_change = True
        lines.append('  + 标签 "{}"'.format(tag))
    for tag in changes["drop_tags"]:
        any_change = True
        lines.append('  - 标签 "{}"'.format(tag))
    if changes["add_coll_names"]:
        any_change = True
        for name in changes["add_coll_names"]:
            lines.append("  + 分类 {}".format(name))
    if changes["drop_coll_names"]:
        any_change = True
        for name in changes["drop_coll_names"]:
            lines.append("  - 分类 {}".format(name))
    if not any_change:
        raise SystemExit("错误：没有指定任何修改（--set / --add-tag / --add-to-collection …）。")
    return lines


def _brief(value):
    if value is None or value == "":
        return "（空）"
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "…"


def _show_value(field, value):
    """预览里展示旧值时，日期要收敛成可读形式，别把 "2024-05-16 2024-05-16" 抛给用户。"""
    if field == "date" and value:
        return display_date(value)
    return value


def cmd_update(args, conn, data_dir):
    key = require(args.key, "zotquery.py update <KEY> --set FIELD=VALUE")
    try:
        current = item_info(conn, data_dir, key)
    except SystemExit:
        raise SystemExit("错误：库里找不到条目 {}。".format(key))
    current["itemID"] = conn.execute(
        "SELECT itemID FROM items WHERE key = ?", (key,)
    ).fetchone()["itemID"]

    changes = collect_updates(args, conn, current)
    lines = render_update_preview(key, current, changes)
    if not write_gate(args, lines, "修改"):
        return 0 if args.dry_run else 3

    api = api_client(opts_base(args))
    try:
        live = api.get_item(key)
        payload = {k: v for k, v in live.items()
                   if k not in ("key", "version", "links", "meta",
                                "dateAdded", "dateModified", "synced", "deleted")}
        for field, _before, after in changes["fields"]:
            if after is None:
                payload.pop(field, None)
            else:
                payload[field] = after
        if changes["add_tags"] or changes["drop_tags"]:
            tags = [{"tag": t["tag"] if isinstance(t, dict) else t}
                    for t in (payload.get("tags") or [])]
            names = [t["tag"] for t in tags]
            for tag in changes["add_tags"]:
                if tag not in names:
                    tags.append({"tag": tag})
            tags = [t for t in tags if t["tag"] not in changes["drop_tags"]]
            payload["tags"] = tags
        if changes["add_colls"] or changes["drop_colls"]:
            colls = list(payload.get("collections") or [])
            for k in changes["add_colls"]:
                if k not in colls:
                    colls.append(k)
            payload["collections"] = [k for k in colls if k not in changes["drop_colls"]]
        result = api.update_item(key, payload)
    except zapi.ApiError as exc:
        return handle_api_error(exc)

    if args.json:
        print(json.dumps(result or {}, ensure_ascii=False, indent=2))
    else:
        print("已修改 [{}]。Zotero 界面会立即刷新。".format(key))
        _print_key_hint(api)
    return 0


# --------------------------------------------------------------------------
# 写操作：删除 / 回收站
# --------------------------------------------------------------------------
def cmd_delete(args, conn, data_dir):
    key = require(args.key, "zotquery.py delete <KEY> [--permanent]")
    row = conn.execute(
        "SELECT itemID FROM items WHERE key = ?", (key,)
    ).fetchone()
    if not row:
        raise SystemExit("错误：库里找不到条目 {}（用 search 确认 KEY）。".format(key))
    info = item_info(conn, data_dir, key)
    head = "  [{}] {} {}".format(key, info["itemType"], info["fields"].get("title", ""))

    if args.permanent:
        lines = [
            "将【永久删除】下列条目，不可恢复：",
            head,
            "",
            "说明：Zotero 本地 API 的 DELETE 会直接抹除记录，不经过回收站。",
        ]
        if not write_gate(args, lines, "永久删除"):
            return 0 if args.dry_run else 3
        api = api_client(opts_base(args))
        try:
            api.delete_item(key)
        except zapi.ApiError as exc:
            return handle_api_error(exc)
        print("已永久删除：[{}]。".format(key))
        return 0

    lines = [
        "将把下列条目移入回收站（可在 Zotero 中还原）：",
        head,
        "",
        "要连回收站一起清掉，请改用 delete --permanent；清空整个回收站用 trash --empty。",
    ]
    if not write_gate(args, lines, "移入回收站"):
        return 0 if args.dry_run else 3

    api = api_client(opts_base(args))
    try:
        api.trash_item(key)
    except zapi.ApiError as exc:
        return handle_api_error(exc)
    print("已移入回收站：[{}]。".format(key))
    return 0


def cmd_trash(args, conn, data_dir):
    if not args.empty:
        rows = conn.execute(
            """SELECT i.key, i.itemID, it.typeName,
                      (SELECT v.value FROM itemData d JOIN itemDataValues v ON v.valueID = d.valueID
                        WHERE d.itemID = i.itemID
                          AND d.fieldID = (SELECT fieldID FROM fields WHERE fieldName = 'title')) AS title
               FROM items i
               JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
               WHERE i.itemID IN (SELECT itemID FROM deletedItems)
                 AND it.typeName NOT IN ('attachment', 'note', 'annotation')
               ORDER BY i.dateModified DESC"""
        ).fetchall()
        if args.json:
            print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
            return 0
        if not rows:
            print("回收站为空。")
            return 0
        print("回收站共 {} 条：".format(len(rows)))
        for r in rows:
            print("  [{}] {} {}".format(r["key"], r["typeName"], r["title"] or "(无标题)"))
        return 0

    lines = [
        "将清空 Zotero 回收站（永久删除，不可恢复）。",
        "",
        "提示：这个操作通过 Zotero 的『清空回收站』接口执行，会删除回收站里的全部条目，"
        "不是单条删除。",
    ]
    if not write_gate(args, lines, "清空回收站"):
        return 0 if args.dry_run else 3
    api = api_client(opts_base(args))
    try:
        api.empty_trash()
    except zapi.ApiError as exc:
        return handle_api_error(exc)
    print("回收站已清空。")
    return 0


def cmd_doctor(args, conn, data_dir):
    report = zapi.probe(opts_base(args))
    report["dataDir"] = data_dir
    entry, tried = zlaunch.find(getattr(args, "exe", None))
    report["program"] = entry
    if not entry:
        report["programTried"] = tried
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get("ok") else 1
    if entry:
        print("Zotero 程序: {}（{}，来源 {}）".format(
            entry["exe"], entry["version"] or "版本未知", entry["source"]))
    else:
        print("Zotero 程序: 未找到（用 --exe 或环境变量 ZOTERO_EXE 指定）")
    print("数据目录: " + str(data_dir))
    if report.get("ok"):
        print("本地 API: 可读（Server-ID={}, API 版本={}）".format(
            report.get("serverId") or "无此头", report.get("apiVersion")))
        # Server-ID 响应头是 Zotero 10 才有的，用它区分 9.x（只读）与 10+（可写）
        if report.get("serverId"):
            print("写入能力: 可用（首次写入时 Zotero 会弹窗授权，建议选「始终允许」）")
        else:
            print("写入能力: 不可用（当前 Zotero 低于 10，本地 API 只读）")
            print("  处理: 升级到 Zotero 10 或更高版本后重试。")
        return 0
    print("本地 API: 不可用")
    print("  原因: " + report.get("message", ""))
    if report.get("hint"):
        print("  处理: " + report["hint"])
    if report.get("stage") == "not_running" and entry:
        print("  启动: zotquery.py launch --yes（会打开 Zotero 窗口，先征得用户同意）")
    return 1


def cmd_locate(args, data_dir):
    """只报告 Zotero 程序装在哪，不启动任何东西。"""
    entry, tried = zlaunch.find(getattr(args, "exe", None))
    _warn_exe_fallback(args, entry)
    report = zapi.probe(opts_base(args))
    if args.json:
        payload = {
            "program": entry,
            "dataDir": data_dir,
            "api": report,
            "tried": tried,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if entry else 1

    if entry:
        print("Zotero 程序: {}".format(entry["exe"]))
        print("  版本: {}".format(entry["version"] or "未知"))
        print("  来源: {}".format(entry["source"]))
    else:
        print("Zotero 程序: 未找到。已尝试：")
        for item in tried:
            print("  - " + item)
        print("  处理: 用 --exe <绝对路径> 指定，或设置环境变量 ZOTERO_EXE。")
    print("数据目录: " + str(data_dir))
    if report.get("ok"):
        print("本地 API: 可读（Server-ID={}）".format(report.get("serverId") or "无此头"))
    else:
        print("本地 API: 不可用（{}）".format(report.get("stage", "unknown")))
        if entry:
            print("  启动: zotquery.py launch --yes")
    return 0 if entry else 1


def cmd_launch(args, data_dir):
    """启动 Zotero 并等本地 API 就绪。启动会打开窗口，因此同样受 --yes 闸门约束。"""
    report = zapi.probe(opts_base(args))

    if report.get("ok"):
        print("Zotero 已在运行（Server-ID={}），无需启动。".format(
            report.get("serverId") or "无此头"))
        return 0

    if report.get("stage") == "disabled":
        print("Zotero 已在运行，但本地 API 未启用。")
        print("  处理: 设置 → 高级 → 勾选「允许其他应用程序在此计算机上与 Zotero 通信」。")
        return 1

    entry, tried = zlaunch.find(getattr(args, "exe", None))
    if not entry:
        print("错误：找不到 Zotero 程序。已尝试：", file=sys.stderr)
        for item in tried:
            print("  - " + item, file=sys.stderr)
        print("可用 --exe <绝对路径> 指定，或设置环境变量 ZOTERO_EXE。", file=sys.stderr)
        return 2
    _warn_exe_fallback(args, entry)

    lines = [
        "将启动 Zotero：",
        "  {}（{}）".format(entry["exe"], entry["version"] or "版本未知"),
        "",
        "启动后会打开 Zotero 窗口，并等待本地 API 就绪（最长 {:.0f} 秒）。".format(
            args.timeout),
    ]
    if args.minimized and zlaunch.IS_WINDOWS:
        lines.append("注意：Windows 下由资源管理器代启动，最小化不保证生效。")
    lines.append("")
    if not write_gate(args, lines, "启动"):
        return 0 if args.dry_run else 3

    print("正在启动：{}".format(entry["exe"]))
    try:
        proc = zlaunch.spawn(entry, minimized=args.minimized)
    except OSError as exc:
        print("错误：启动失败 —— {}".format(exc), file=sys.stderr)
        return 1

    if args.no_wait:
        print("已发起启动，未等待就绪。")
        return 0

    print("等待本地 API 就绪（最长 {:.0f} 秒）……".format(args.timeout))
    api = zapi.LocalApi(base=opts_base(args) or zapi.DEFAULT_BASE, timeout=5)
    started = time.time()
    ok, stage, detail = zlaunch.wait_ready(
        api.ping, timeout=args.timeout, proc=proc)
    elapsed = time.time() - started

    if ok:
        print("就绪（耗时 {:.1f} 秒）。Server-ID={}，API 版本={}".format(
            elapsed, detail.get("serverId") or "无此头", detail.get("apiVersion")))
        return 0

    if stage == "disabled":
        print("Zotero 已启动，但本地 API 未启用。")
        print("  处理: 设置 → 高级 → 勾选「允许其他应用程序在此计算机上与 Zotero 通信」。")
        return 1
    if stage == "exited":
        print("错误：Zotero 启动后立即退出。{}".format(detail), file=sys.stderr)
        return 1

    print("超时：{:.0f} 秒后本地 API 仍无响应。".format(args.timeout), file=sys.stderr)
    locks = zlaunch.stale_locks()
    if locks:
        print("  检测到 profile 锁文件（Zotero 上次可能异常退出，会让新实例直接退出）：",
              file=sys.stderr)
        for path in locks:
            print("    " + path, file=sys.stderr)
        print("  确认 Zotero 确实没在运行后，删除该文件再重试。", file=sys.stderr)
    print("  也可能是首次启动正在升级数据库（会弹对话框，需要在窗口里点确认），", file=sys.stderr)
    print("  或本地 API 未启用，或启动被安全软件拦截。", file=sys.stderr)
    print("  排查: zotquery.py doctor", file=sys.stderr)
    return 1


def opts_base(args):
    return getattr(args, "api_base", None)


def _warn_exe_fallback(args, entry):
    """--exe 指定了但没生效时说明一句，避免用户误以为用的是自己给的路径。"""
    hint = getattr(args, "exe", None)
    if hint and entry and entry.get("source") != "参数 --exe":
        print("注意：--exe 指定的路径不可用，已改用自动探测结果。", file=sys.stderr)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def add_filter_args(p, date_default=None):
    p.add_argument("--type", help="条目类型，如 journalArticle / thesis / book")
    p.add_argument("--collection", help="分类名称或完整路径（含子分类）")
    p.add_argument("--tag", help="按标签精确过滤")
    p.add_argument("--since", metavar="DATE", help="条目日期下界：2020 / 2020-05 / 2020-05-16")
    p.add_argument("--until", metavar="DATE", help="条目日期上界（含该粒度整天/整年）")
    p.add_argument("--added", metavar="RANGE", help="入库时间，如 2026-01-01 或 2026-01-01..2026-06-30")
    p.add_argument("--modified", metavar="RANGE", help="最近修改时间，格式同 --added")
    p.add_argument("--untagged", action="store_true", help="只看没打标签的条目")
    p.add_argument("--missing", metavar="FIELD",
                   help="只看缺某字段的条目：doi / abstract / date / tag / collection / pdf")
    p.add_argument("--sort", default=date_default or "date", choices=list(SORT_FIELDS))
    p.add_argument("--order", default="desc", choices=["asc", "desc"])
    p.add_argument("--offset", type=int, default=0, help="跳过前 N 条（分页）")


def add_write_args(p):
    p.add_argument("--yes", action="store_true", help="确认执行（不加则只输出预览）")
    p.add_argument("--dry-run", action="store_true", help="只预览，一定不执行")
    p.add_argument("--api-base", help="本地 API 地址，默认 http://127.0.0.1:23119/api")


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", help="Zotero 数据目录（或 zotero.sqlite 路径）")
    common.add_argument("--json", action="store_true", help="输出 JSON")
    common.add_argument("--fresh", action="store_true", help="忽略快照缓存，强制重新复制")

    parser = argparse.ArgumentParser(
        prog="zotquery.py",
        description="本地 Zotero 文献库查询与编辑工具（查走只读快照，增删改走官方本地 API）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  zotquery.py search \"社会流动\" --since 2020 --in title,abstract\n"
               "  zotquery.py list --missing pdf --sort added\n"
               "  zotquery.py info ABCD1234\n"
               "  zotquery.py update ABCD1234 --set DOI=10.1234/x --add-tag 已读\n"
               "  zotquery.py delete ABCD1234 --yes\n"
               "  zotquery.py doctor",
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("collections", parents=[common], help="列出分类")
    p.add_argument("--library", type=int, help="仅显示某个 libraryID 的分类")

    p = sub.add_parser("list", parents=[common], help="列出条目")
    add_filter_args(p)
    p.add_argument("--limit", type=int, default=30, help="最多返回条数（默认 30，0 表示不限）")

    p = sub.add_parser("search", parents=[common], help="检索条目")
    p.add_argument("query", nargs="?", help="关键词")
    p.add_argument("--in", dest="in_scopes", action="append", metavar="DOMAIN",
                   help="检索域，可多次指定或逗号分隔：title/creator/tag/publication/"
                        "abstract/doi/callnumber/notes/annotations/fulltext")
    p.add_argument("--deep", action="store_true", help="等价于 title,creator,tag,publication,abstract,doi")
    p.add_argument("--everything", action="store_true", help="连笔记、批注、PDF 全文一起搜")
    p.add_argument("--exact", action="store_true", help="整字段精确匹配，而非子串")
    add_filter_args(p)
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("info", parents=[common], help="条目完整详情")
    p.add_argument("key", nargs="?", help="条目 KEY（附件/笔记 KEY 会自动回溯父条目）")
    p.add_argument("--max-note", type=int, default=4000, help="笔记最多输出多少字（0=不限）")

    p = sub.add_parser("text", parents=[common], help="读取全文")
    p.add_argument("key", nargs="?", help="条目 KEY")
    p.add_argument("--att", help="指定附件 KEY（默认自动选 PDF）")
    p.add_argument("--max-chars", type=int, default=12000, help="最多输出多少字（0=不限）")

    p = sub.add_parser("tags", parents=[common], help="列出标签")
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("add", parents=[common], help="新增条目（需 --yes）")
    add_write_args(p)
    p.add_argument("--type", default="journalArticle", help="条目类型，默认 journalArticle；note 表示笔记")
    p.add_argument("--title")
    p.add_argument("--creator", action="append", metavar="NAME",
                   help='作者，可多次。格式 "姓, 名" 或 "整名"，可加 #角色 如 "Smith, J#editor"')
    p.add_argument("--set", action="append", metavar="FIELD=VALUE", help="设置字段，可多次")
    p.add_argument("--tag", action="append", help="标签，可多次")
    p.add_argument("--collection", action="append", help="加入分类，可多次")
    p.add_argument("--parent", help="父条目 KEY（笔记/子条目用）")
    p.add_argument("--text", help="笔记正文（配合 --type note）")
    p.add_argument("--from-json", metavar="FILE",
                   help="直接提交 Zotero 条目 JSON（文件路径，- 表示 stdin）")

    p = sub.add_parser("update", parents=[common], help="修改条目（需 --yes）")
    add_write_args(p)
    p.add_argument("key")
    p.add_argument("--set", action="append", metavar="FIELD=VALUE")
    p.add_argument("--unset", action="append", metavar="FIELD")
    p.add_argument("--add-tag", action="append")
    p.add_argument("--remove-tag", action="append")
    p.add_argument("--add-to-collection", action="append")
    p.add_argument("--remove-from-collection", action="append")

    p = sub.add_parser("delete", parents=[common], help="移入回收站（需 --yes）")
    add_write_args(p)
    p.add_argument("key")
    p.add_argument("--permanent", action="store_true",
                   help="永久删除，不经回收站（不可恢复）")

    p = sub.add_parser("trash", parents=[common], help="查看或清空回收站")
    add_write_args(p)
    p.add_argument("--empty", action="store_true", help="清空回收站（不可恢复，需 --yes）")

    doctor = sub.add_parser(
        "doctor", parents=[common], help="检查程序位置、数据目录与写入通道状态")
    doctor.add_argument(
        "--api-base", help="本地 API 地址，默认 http://127.0.0.1:23119/api")
    doctor.add_argument("--exe", help="Zotero 可执行文件路径（自动探测失败时用）")

    locate = sub.add_parser(
        "locate", parents=[common], help="显示 Zotero 程序装在哪、数据目录在哪")
    locate.add_argument("--api-base", help="本地 API 地址")
    locate.add_argument("--exe", help="Zotero 可执行文件路径（自动探测失败时用）")

    launch = sub.add_parser(
        "launch", parents=[common], help="启动 Zotero 并等本地 API 就绪（需 --yes）")
    add_write_args(launch)
    launch.add_argument("--exe", help="Zotero 可执行文件路径（自动探测失败时用）")
    launch.add_argument("--timeout", type=float, default=90.0,
                        help="等待本地 API 就绪的秒数，默认 90")
    launch.add_argument("--minimized", action="store_true",
                        help="最小化启动（Windows 下由资源管理器代启动，不保证生效）")
    launch.add_argument("--no-wait", action="store_true",
                        help="发起启动后立即返回，不等待 API 就绪")

    return parser


def require(value, usage):
    if not value:
        print("错误：缺少参数。用法：" + usage, file=sys.stderr)
        raise SystemExit(2)
    return value


def main(argv=None):
    _force_utf8_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    if args.command in ("doctor", "locate", "launch"):
        data_dir, _ = locate_data_dir(args.data_dir)
        label = data_dir or "(未定位到)"
        if args.command == "doctor":
            return cmd_doctor(args, None, label)
        if args.command == "locate":
            return cmd_locate(args, label)
        return cmd_launch(args, label)

    data_dir, origin = locate_data_dir(args.data_dir)
    if not data_dir:
        message = ["错误：无法定位 Zotero 数据目录。已尝试："]
        message += ["  - " + t for t in origin]
        message.append("请用 --data-dir <目录> 指定，或设置环境变量 ZOTERO_DATA_DIR。")
        raise SystemExit("\n".join(message))

    snapshot, _, _ = build_snapshot(data_dir, use_cache=not args.fresh)
    conn = connect(snapshot)
    try:
        return dispatch(args, conn, data_dir)
    finally:
        conn.close()


def dispatch(args, conn, data_dir):
    cmd = args.command

    if cmd == "collections":
        info = collection_table(conn)
        nodes = sorted(info.values(), key=lambda n: (n["libraryID"], n["path"]))
        if args.library:
            nodes = [n for n in nodes if n["libraryID"] == args.library]
        if args.json:
            print(json.dumps(nodes, ensure_ascii=False, indent=2))
        else:
            for n in nodes:
                print("{}{}  ({} 条)  [#{}]".format(
                    "  " * n["depth"], n["name"], n["count"], n["id"]))
            if not nodes:
                print("（没有分类）")
        return 0

    if cmd == "list":
        rows = fetch_items(conn)
        rows = apply_filters(conn, rows, args)
        rows = sort_rows(conn, rows, args.sort, args.order)
        total = len(rows)
        rows = _paginate(rows, args.limit, args.offset)
        if args.json:
            print(json.dumps({"total": total, "offset": args.offset,
                              "items": _public(rows)},
                             ensure_ascii=False, indent=2, default=str))
        else:
            for line in describe(rows, conn):
                print(line)
            _print_page_footer(total, rows, args)
        return 0

    if cmd == "search":
        query = require(args.query, 'zotquery.py search "关键词"')
        scopes = normalize_scopes(args.in_scopes, args.deep, args.everything)
        rows = search_items(conn, data_dir, query, scopes, args.exact)
        rows = apply_filters(conn, rows, args)
        rows = sort_rows(conn, rows, args.sort, args.order)
        total = len(rows)
        rows = _paginate(rows, args.limit, args.offset)
        if args.json:
            print(json.dumps({"query": query, "scopes": scopes, "total": total,
                              "offset": args.offset, "items": _public(rows)},
                             ensure_ascii=False, indent=2, default=str))
        else:
            if not rows:
                print("未命中：{} 域中都没有匹配「{}」的条目。".format(
                    "/".join(scopes), query))
                return 0
            for line in describe(rows, conn):
                print(line)
            _print_page_footer(total, rows, args)
        return 0

    if cmd == "info":
        key = require(args.key, "zotquery.py info <KEY>")
        info = item_info(conn, data_dir, key)
        if args.json:
            print(json.dumps(info, ensure_ascii=False, indent=2, default=str))
        else:
            print(render_info(info, max_note=args.max_note))
        return 0

    if cmd == "text":
        key = require(args.key, "zotquery.py text <KEY>")
        info = item_info(conn, data_dir, key)
        att = pick_attachment(info["attachments"], args.att)
        if not att:
            print("错误：条目 {} 没有可读的附件文件。可用附件：".format(info["key"]),
                  file=sys.stderr)
            for a in info["attachments"]:
                print("  [{}] {} exists={}".format(a["key"], a["path"], a["exists"]),
                      file=sys.stderr)
            return 1
        content, source = read_fulltext(att)
        if content is None:
            print("该附件没有全文索引缓存，也不是可直接解析的 HTML：", file=sys.stderr)
            print("  " + str(att["path"]), file=sys.stderr)
            print("请用你的 PDF/文档读取能力直接打开该文件。", file=sys.stderr)
            return 1
        content = clip(content, args.max_chars, "用 --max-chars 0 读完整文本")
        if args.json:
            print(json.dumps(
                {"key": info["key"], "attachment": att["key"], "source": source,
                 "text": content}, ensure_ascii=False, indent=2))
        else:
            print("# 来源: {} ({})".format(source, att["key"]), file=sys.stderr)
            print(content)
        return 0

    if cmd == "tags":
        rows = conn.execute(
            """SELECT t.name, COUNT(*) AS n FROM itemTags ig
               JOIN tags t ON ig.tagID = t.tagID
               WHERE ig.itemID NOT IN (SELECT itemID FROM deletedItems)
               GROUP BY t.tagID ORDER BY n DESC, t.name LIMIT ?""",
            (args.limit,),
        ).fetchall()
        if args.json:
            print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
        else:
            for r in rows:
                print("{}  {}".format(
                    " " * max(0, 4 - _width(str(r["n"]))) + str(r["n"]), r["name"]))
        return 0

    if cmd == "add":
        return cmd_add(args, conn, data_dir)
    if cmd == "update":
        return cmd_update(args, conn, data_dir)
    if cmd == "delete":
        return cmd_delete(args, conn, data_dir)
    if cmd == "trash":
        return cmd_trash(args, conn, data_dir)

    raise SystemExit("未知命令：" + cmd)


def _paginate(rows, limit, offset):
    offset = max(0, offset or 0)
    rows = rows[offset:]
    if limit and limit > 0:
        rows = rows[:limit]
    return rows


def _print_page_footer(total, shown, args):
    if total > len(shown) + (args.offset or 0):
        print("—— 共 {} 条，当前第 {}–{} 条（--limit / --offset 翻页）".format(
            total, (args.offset or 0) + 1, (args.offset or 0) + len(shown)))
    elif total > len(shown):
        print("—— 共 {} 条，已显示 {} 条".format(total, len(shown)))


if __name__ == "__main__":
    sys.exit(main())
