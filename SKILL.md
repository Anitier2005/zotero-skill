---
name: zotero-local
description: Read and edit the user's local Zotero library. Use when asked to find, read, summarize, compare, cite, or quote literature from their own Zotero library (search items, read metadata/abstracts/notes/PDF highlights, locate full text, build reference lists), or to add, modify, tag, file, or delete entries in it. Any create/update/delete requires showing the user a preview and getting explicit approval first. 读写本地 Zotero 文献库：检索、读元数据与摘要、读笔记批注、定位 PDF 全文、生成参考文献，以及新增/修改/打标签/归类/删除条目（写入前必须先让用户确认预览）。
license: MIT
metadata:
  version: 3.0.0
  requires: Python 3.8+ (standard library only); Zotero 10+ for writes
  platforms: Windows, macOS, Linux
---

# zotero-local — 读写本地 Zotero 文献库

**查**走只读快照（直接读 SQLite 副本，Zotero 关着也能用）。
**增删改**走 Zotero 官方本地 API（让 Zotero 自己落盘，避免弄坏库）。

两条通道的分工不是实现细节，是安全边界：绝不要用 SQL 去改 `zotero.sqlite`，原因见 `references/write-protocol.md`。

## 一、查（随时可用）

脚本路径相对**本 SKILL.md 所在目录**：

```bash
python3 <skill_dir>/scripts/zotquery.py <命令> [选项]     # macOS / Linux
python  <skill_dir>\scripts\zotquery.py <命令> [选项]     # Windows
```

| 命令 | 用途 |
|---|---|
| `collections` | 分类树，含层级路径、条目数、libraryID |
| `list` | 列条目：`--type` `--collection` `--tag` `--since` `--until` `--added` `--modified` `--untagged` `--missing pdf\|doi\|abstract` `--sort` `--offset` |
| `search <词>` | 检索：`--in title,creator,tag,publication,abstract,doi,callnumber,notes,annotations,fulltext`、`--deep`、`--everything`、`--exact` |
| `info <KEY>` | 全字段、作者、标签、分类、附件路径、笔记、批注 |
| `text <KEY>` | 正文：索引缓存优先，HTML 快照次之；`--att` 指定附件 |
| `tags` | 标签频次 |
| `trash` | 查看回收站条目；`--empty` 清空整个回收站 |
| `delete <KEY>` | 移入回收站；加 `--permanent` 永久删除 |

全局：`--data-dir` `--json` `--fresh` `--version`。

**检索域怎么选**：默认只搜标题/作者/标签/出处（等价 Zotero 快速搜索）。要找"哪篇文献讨论过某概念"，用 `--in abstract` 或 `--everything`；`--everything` 会连笔记、批注、PDF 全文一起扫，慢但全。

**结果可信度**：截断必带 `…[已截断：原文 N 字…]`，看到就说明没读全，用 `--max-note 0` / `--max-chars 0` 取全量。查不到就直说"库中未找到"，不要编造。

## 二、增删改（必须先要用户同意）

### 前置条件

1. Zotero 正在运行；
2. 设置 → 高级 → 勾选「允许其他应用程序在此计算机上与 Zotero 通信」；
3. Zotero **10 或更高**（10 之前本地 API 只读）。

先跑 `doctor` 确认通道状态，不要把"连不上"当成"库是空的"。

### 强制流程（不可跳过）

```
第 1 步：不加 --yes 运行，只拿预览
第 2 步：把预览原文完整展示给用户，等明确的"可以/执行/同意"
第 3 步：用户同意后，同一条命令加 --yes 执行
```

不加 `--yes` 时脚本**只打印变更预览并返回退出码 3，不发送任何请求**。这是刻意的：预览是给你拿去问用户的材料，不是给你自己判断的依据。

写操作可以批量，但一次只提一类变更（本次改字段、本次加标签…），不要把一个"顺手整理"打包成十几条改动让用户一次性放行。

```bash
# 新增
zotquery.py add --type journalArticle --title "标题" \
    --creator "白春玲" --creator "陈东" \
    --set DOI=10.xxxx/yyy --set date=2024-05-16 \
    --tag 待读 --collection 我的分类
# 修改
zotquery.py update ABCD1234 --set DOI=10.xxxx --add-tag 已读 --add-to-collection 我的分类
zotquery.py update ABCD1234 --unset DOI --remove-tag 待读
# 新增笔记
zotquery.py add --type note --parent ABCD1234 --text "要点……"
# 删除（默认进回收站，可还原）
zotquery.py delete ABCD1234
# 永久删除单条（不经回收站，不可恢复）
zotquery.py delete ABCD1234 --permanent
# 清空回收站（不可恢复，会清掉整个回收站）
zotquery.py trash --empty
```

`--set` 支持 Zotero 字段名，也认中文标签（`--set 期刊=经济研究`）。`--creator` 格式 `"姓, 名"` 或 `"整名"`，可用 `#角色` 指定，如 `"Smith, J#editor"`。

### 删除的边界

- `delete` 默认 = 移入回收站（内部走 `PATCH deleted=1`），用户可还原；
- `delete --permanent` = 永久抹除单条（内部走 `DELETE`，Zotero 执行 `eraseTx`）。**这是本地 API 与 zotero.org Web API 的关键差异**：Web API 的 DELETE 是进回收站，本地 API 的 DELETE 是直接抹掉；
- `trash --empty` 清的是**整个回收站**，会连带用户以前删掉的所有条目，执行前必须再次确认；
- 删除父条目会连带其附件与笔记，预览里要说明。

## 三、红线

1. **绝不直接写 SQLite**。只读快照怎么读都行，写只走本地 API。
2. **绝不跳过确认**。没有用户明确的"同意"，不加 `--yes`。
3. **不脑补内容**。引用观点前必须真的读到文本；查不到就说查不到。
4. **不倾倒原文**。除用户要求，不整段输出全文，以摘要式引用为主。
5. **不外传**。元数据、笔记、批注、全文都是用户私有数据。

## 四、排错

| 现象 | 处理 |
|---|---|
| `无法定位 Zotero 数据目录` | `--data-dir "<绝对路径>"` 或设 `ZOTERO_DATA_DIR`；脚本会列出尝试过的候选 |
| 刚加的条目查不到 | 快照按 mtime/size 复用，正常会自动重建；仍查不到用 `--fresh` |
| 写入报 `连不上本地 API` | Zotero 没启动 |
| 写入报 `403 / 本地 API 未启用` | 设置 → 高级 → 勾选「允许其他应用程序…」 |
| 写入报 `404/405，需 Zotero 10+` | 当前是 Zotero 9 及更早，升级后才能写 |
| 写入报 `412 已被其他程序修改` | 条目在此期间被改过；重新读取再试，别硬覆盖 |
| 写入报 `429 授权过于频繁` | 一分钟内最多弹 5 次授权窗，稍等 |
| 附件 `exists=false` | 链接附件或文件已转移；如实告知，不要假装读过 |
| 全文里字间夹空格 | 部分中文 PDF 的索引缓存逐字分隔；语义可读，逐字引用请开原 PDF |

细节：SQL 表结构见 `references/zotero-schema.md`，写入协议与错误码见 `references/write-protocol.md`。
