# zotero-skill

让 AI agent 直接读写你本机的 Zotero 文献库 —— 检索条目、读摘要与笔记、定位 PDF 全文、查 PDF 高亮批注，以及新增/修改/打标签/归类/删除。

**核心设计：查走只读快照，改走官方本地 API。绝不直写 SQLite。**

## 为什么需要它

Zotero 已有的接入方式各有短板：

| 方式 | 问题 |
|---|---|
| zotero.org Web API | 需要联网、要申请 API key、依赖库已同步 |
| 第三方 MCP 服务 | 多数只读，且要额外跑一个进程 |
| 直接改 `zotero.sqlite` | Zotero 运行时把库缓存在内存，绕过去写会被覆盖、触发同步冲突，严重时损坏库 |

这个工具的做法是：

- **读** → 把 `zotero.sqlite`（连同 `-wal` / `-journal`）复制成只读快照再查。Zotero 关着也能用，绝不碰主库。
- **写** → 走 Zotero 官方本地 API（`127.0.0.1:23119`），让 Zotero 自己落盘、自己维护 `version` 与同步状态。

## 能力

**查**（无版本要求，随时可用）

| 命令 | 用途 |
|---|---|
| `collections` | 分类树：层级路径、条目数、libraryID |
| `list` | 列条目，支持按类型/分类/标签/日期/缺项过滤 |
| `search` | 检索标题、作者、标签、出处、摘要、DOI、笔记、批注、PDF 全文 |
| `info` | 全字段 + 作者角色 + 标签 + 分类路径 + 附件真实路径 + 笔记 + 批注 |
| `text` | 正文：优先读 Zotero 全文索引缓存，HTML 快照次之 |
| `tags` | 标签频次 |
| `trash` | 查看回收站 |

**改**（需 Zotero 10+）

`add` / `update` / `delete` / `trash --empty`，覆盖条目、字段、标签、分类归属、子笔记。

## 环境要求

- **Python 3.8+**，只用标准库，无需 pip 安装任何包
- **读**：任何 Zotero 版本；Zotero 未运行也可以
- **写**：Zotero **10 或更高**（10 之前本地 API 只读），并勾选 设置 → 高级 → 「允许其他应用程序在此计算机上与 Zotero 通信」

## 安装

克隆到你所用的 agent 的 skills 目录，目录名建议保持 `zotero-local`（与 `SKILL.md` 的 `name` 一致）：

```bash
# WorkBuddy
git clone https://github.com/Anitier2005/zotero-skill.git ~/.workbuddy/skills/zotero-local

# Claude Code
git clone https://github.com/Anitier2005/zotero-skill.git ~/.claude/skills/zotero-local
```

任何支持 `SKILL.md` 约定的 agent 都适用：把仓库放进对应的 skills 目录即可，agent 会读取 `SKILL.md` 的 frontmatter 判断何时触发。

## 快速开始

```bash
# 1. 检查通道状态（写入是否可用）
python3 scripts/zotquery.py doctor

# 2. 看库是怎么分类的
python3 scripts/zotquery.py collections

# 3. 找文献
python3 scripts/zotquery.py search "社会流动" --limit 5
python3 scripts/zotquery.py search 代际 --in abstract --since 2020

# 4. 看详情（元数据、笔记、PDF 批注都在里面）
python3 scripts/zotquery.py info ABCD1234

# 5. 读正文
python3 scripts/zotquery.py text ABCD1234 --max-chars 3000
```

Windows 下把 `python3` 换成 `python`，路径分隔符用 `\`。

## 写操作的安全机制

所有写命令默认**只出预览、不执行**：

```bash
$ python3 scripts/zotquery.py update ABCD1234 --add-tag 待读
将修改 [ABCD1234] 某篇论文的标题

  + 标签 "待读"

—— 未执行，以上为变更预览。

$ echo $?
3
```

确认无误后加 `--yes` 才真正发送请求。这是刻意设计的三层闸门：

1. **脚本层**：不加 `--yes` 时退出码为 3，一个请求都不发
2. **Zotero 层**：首次写入会弹出授权框，可选「始终允许」长期授权
3. **Agent 层**：`SKILL.md` 把「先给用户看预览、拿到明确同意再执行」写成硬规则

删除语义有三个不同的操作，别混淆：

| 命令 | 内部实现 | 后果 |
|---|---|---|
| `delete <KEY>` | `PATCH {"deleted": 1}` | 进回收站，可在 Zotero 中还原 |
| `delete <KEY> --permanent` | `DELETE`（Zotero 执行 `eraseTx`） | 永久抹除，不可恢复 |
| `trash --empty` | `DELETE /items/trash` | 清空**整个**回收站，不可恢复 |

> 注意：Zotero 本地 API 的 `DELETE` 是**永久删除**，与 zotero.org Web API「DELETE = 进回收站」的语义相反。这是本工具在真机测试中发现并修正的关键差异。

## 数据目录定位

脚本按以下顺序查找，找不到会列出所有尝试过的候选路径：

1. 命令行 `--data-dir "<绝对路径>"`
2. 环境变量 `ZOTERO_DATA_DIR`
3. 解析 Zotero 的 `prefs.js`
4. 回退到 `~/Zotero`

## 运行时会在系统里留下什么

只有两处，都可随时删除：

- `%TEMP%/zotquery-cache/<hash>/`（或 `/tmp/zotquery-cache/`）—— 数据库只读快照，按库文件 mtime/size 自动复用与重建
- `%LOCALAPPDATA%/zotero-local/write-key.json` —— 仅在用户选「始终允许」时生成，用于免重复授权

不写注册表，不改 Zotero 配置，不往用户目录塞其他东西。

## 已知限制

- **部分中文 PDF 的全文索引缓存逐字带空格**（PDF 排版所致，非脚本问题）。语义可读，但逐字引用建议打开原 PDF。
- **本地 API 没有「永久删除单条以外」的批量接口**。批量删除需逐条调用。
- **回收站中的条目在查命令里默认不可见**，用 `trash` 单独查看。
- **只在 Zotero 10.0.6 / Windows 上做过完整真机验证**。Linux 与 macOS 路径已按平台差异实现，但未实测。

## 项目结构

```
.
├── SKILL.md                        # agent 入口：何时触发、命令表、红线、排错
├── scripts/
│   ├── zotquery.py                 # CLI：查询（SQLite 只读快照）+ 写命令
│   └── zapi.py                     # Zotero 本地 API 客户端（授权、重试、错误码）
└── references/
    ├── zotero-schema.md            # SQLite 表结构速查（按需加载）
    └── write-protocol.md           # 写入协议与错误码（按需加载）
```

`references/` 采用渐进式披露：只有当内置命令不够用、需要手写 SQL 或排查 API 错误时才会被读取，避免把细节塞进主文件。

## License

MIT
