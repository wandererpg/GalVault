# GalVault

GalVault 是一个用于整理、检索和筛选 Galgame / 视觉小说资料的 Obsidian 知识库。

## 内容

- 作品、发行版、公司、创作者、角色、声优和标签
- 作品年份、评分、排名、年龄分级、系列关系和动画改编记录
- 作品封面和角色画像（WebP 压缩格式）
- 角色中文显示标题，以及罗马音、日文原名和别名检索
- 可通过 Obsidian 内部链接和 Bases 视图进行交叉查询

详细的目录结构、字段说明、检索方法和维护规则，请阅读[知识库介绍](知识库介绍.md)。

## 本次更新

### 1. 新增作品封面和角色画像

- 从 VNDB 导入 2,379 张作品封面和 14,960 张角色画像；
- 图片统一转换为 WebP，在尽量保持清晰度的同时减小仓库体积；
- 作品页面新增 `cover_image`，角色页面新增 `portrait_image`，并可在 Obsidian 中直接预览；
- VNDB 暂无图片的 11 部作品和 547 个角色会保留原页面，不使用错误图片替代。

导入方式与注意事项见[[系统/图片导入说明|VNDB 图片导入说明]]，完成情况见[[系统/图片导入/导入报告|图片导入报告]]。

### 2. 新增角色中文显示标题

- 为 15,507 个角色页面生成中文显示标题，并同步更新页面 H1；
- 罗马音文件名、VNDB ID 和现有 Wikilink 均保持不变，避免破坏原有关系；
- 罗马音、日文原名、旧姓及 VNDB 别名写入 `aliases`，仍可通过原名称搜索；
- 其中 8,542 个名称依据日文汉字转换，6,902 个使用假名或罗马音音译，63 个使用常用称谓词典。

中文标题属于机器生成的检索候选，不代表官方中文译名。转换说明见[[系统/角色中文化/translation|角色中文标题转换结果]]；发现错误时，可在[[系统/角色中文化/中文名修订.csv|中文名修订表]]中更正。

## 目录

| 目录 | 内容 |
| --- | --- |
| `作品` | 视觉小说主条目 |
| `发行版` | PC、主机、下载版和语言版本 |
| `会社` | 制作公司与发行商 |
| `创作者` | 编剧、原画、音乐、声优等人员 |
| `角色` | 角色及其作品、声优关联 |
| `标签` | 作品标签与标签定义 |
| `系统` | 验证报告和系统文件 |
| `assets` | 作品封面与角色画像 |

## 使用方式

1. 使用 Obsidian 打开本目录作为 Vault。
2. 通过全局搜索或根目录中的 `.base` 文件筛选资料。
3. 打开作品页面，沿着 `[[内部链接]]` 查询相关公司、角色、声优和标签。

## 更新图片

新增作品或角色后，可运行：

```powershell
python 系统/工具/import_vndb_images.py
```

脚本会复用已有文件，只下载新增或先前失败的图片。详细参数见[图片导入说明](系统/图片导入说明.md)。

## 更新角色中文标题

首次使用先安装转换依赖，然后运行：

```powershell
python -m pip install -r 系统/工具/character-name-requirements.txt
python 系统/工具/localize_character_names.py
```

脚本从 VNDB 补齐原文名和别名，并生成简体中文显示标题。角色文件名和内部链接不会改变。自动音译不等同于官方译名；可在 `系统/角色中文化/中文名修订.csv` 中加入人工修订后重跑。详见[生成报告](系统/角色中文化/生成报告.md)。

## GitHub 自动上传

本知识库使用 SSH 连接到 `git@github.com:wandererpg/GalVault.git`，默认分支为 `main`。首次克隆时使用：

```powershell
git clone git@github.com:wandererpg/GalVault.git
```

`系统/工具/galvault-auto-sync.ps1` 会把所有未被 `.gitignore` 排除的新增、修改和删除提交后推送到 GitHub。脚本不会强制推送；如果远端出现本地尚未合并的提交，会停止并写入日志，避免覆盖远端内容。

在 Windows 上安装每 10 分钟运行一次的自动任务：

```powershell
powershell -ExecutionPolicy Bypass -File .\系统\工具\install-galvault-auto-sync.ps1
```

也可以指定间隔（单位：分钟），或移除任务：

```powershell
powershell -ExecutionPolicy Bypass -File .\系统\工具\install-galvault-auto-sync.ps1 -IntervalMinutes 30
powershell -ExecutionPolicy Bypass -File .\系统\工具\install-galvault-auto-sync.ps1 -Uninstall
```

任务仅在当前 Windows 用户登录期间运行，并在登录时补跑。运行日志位于 `%LOCALAPPDATA%\GalVault\auto-sync.log`。`.obsidian/`、ZIP 分发包、缓存、依赖、数据库和本机二进制文件不会上传；`GalVault-knowledge-base-2026-09-04.zip` 适合放到 GitHub Release，而不是作为普通仓库文件提交。

本仓库主要用于资料整理和作品筛选，不保证外部评分、发行状态或语言信息永久不变。涉及官方中文、动画化和发行平台的信息，应以官方来源为准。
