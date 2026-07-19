<div align="center">

<img src="save-papers-to-zotero/assets/icon.png" alt="保存论文到 Zotero 图标" width="180">

# 保存论文到 Zotero

**一个安全优先的 Codex 与 Claude Code 技能，可将研究论文导入你指定的准确 Zotero 集合。**

[![Tests](https://github.com/AlexXPZhu/save-papers-to-zotero/actions/workflows/tests.yml/badge.svg)](https://github.com/AlexXPZhu/save-papers-to-zotero/actions/workflows/tests.yml)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Codex Skill](https://img.shields.io/badge/Codex-Skill-111827)](save-papers-to-zotero/)
[![Claude Code Skill](https://img.shields.io/badge/Claude%20Code-Skill-D97757)](save-papers-to-zotero/)
[![Zotero](https://img.shields.io/badge/Zotero-Local%20Connector-CC2936?logo=zotero&logoColor=white)](https://www.zotero.org/)

[English](README.md) · [简体中文](README.zh-CN.md)

[解决的问题](#为什么需要这个技能) · [快速开始](#快速开始) · [使用示例](#使用示例) · [安全保证](#安全与行为保证) · [故障排查](#故障排查)

</div>

> [!NOTE]
> 这是一个独立的社区项目，与 Zotero、OpenAI 或 Anthropic 无隶属关系，也未获得其官方背书。

## 为什么需要这个技能

这个技能源于科研调研中的一个实际断点：ChatGPT、Codex 和 Claude Code 可以帮助研究者围绕主题找到有价值的论文，也可以控制 Chrome 打开文章，但调用 Zotero Connector 浏览器扩展并不能提供可验证、可重复且能准确进入目标集合的导入流程。

本技能通过 Zotero 本地 Connector 服务补上了这个缺口。某种程度上，它使得你可以直接在和ChatGPT聊天的过程中就把Zotero Connector的活干了。它不只是下载 PDF，还会创建结构化的 Zotero 条目，保存可靠的元数据，将 arXiv `Comments` 等信息保留为子笔记，添加科研工作流标签，存储 PDF，并最终验证条目和附件是否确实保存成功。

| 没有结构化流程时 | 使用本技能后 |
| --- | --- |
| 条目可能进入错误的集合 | 每次写入前都会解析并再次核验准确目标 |
| 请求成功但可能没有可用的 PDF | 会独立下载并验证已存储的附件 |
| Comments、笔记和阅读状态容易丢失 | 统一添加子笔记和兼容 Ethereal Style 的标签 |
| 批量任务中断后难以安全恢复 | 清单、账本和锁使串行导入可恢复 |
| 重复条目的处理可能具有破坏性或不透明 | 只报告可能重复项，不删除、合并或替换任何现有条目 |


## 功能亮点

- 将单篇论文或一批论文导入准确的 Zotero 集合。
- 保留完整元数据，并将 arXiv Comments 保存为子笔记。
- 与 Ethereal Style Zotero 插件集成，生成其使用的 `#status/...` 和 `#priority/...` 工作流标签，但不会自行猜测优先级。
- 同时验证集合归属和本地存储的 PDF。
- 报告可能重复项，同时执行用户明确要求的新建操作。
- 使用可恢复的批量账本和逐清单锁。
- 仅使用 Python 标准库，无需安装第三方包。

## 快速开始

### 1. 检查要求

| 要求 | 说明 |
| --- | --- |
| 智能体 | 支持技能的 Codex，或 Claude Code 2.1.211 及以上版本 |
| Claude 浏览器访问 | 可选：Claude in Chrome 扩展 1.0.36 及以上版本，并使用 Anthropic 直连方案 |
| Python | Python 3.10 或更高版本；无需第三方包 |
| Zotero | Zotero 桌面版必须正在运行 |
| 本地 API | 在 Zotero 设置中启用 **允许此计算机上的其他应用程序与 Zotero 通信** |
| Ethereal Style | 推荐配套使用的 Zotero 插件，可将生成的 `#status/...` 和 `#priority/...` 标签用于科研工作流 |
| PDF 访问权限 | 你必须已经拥有合法的 PDF 访问权限 |

### 2. 为 Codex 或 Claude Code 安装

#### Codex

最简单的方法是让内置的 `$skill-installer` 直接从 GitHub 安装：

```text
Use $skill-installer to install:
https://github.com/AlexXPZhu/save-papers-to-zotero/tree/main/save-papers-to-zotero
```

下一个 Codex 对话轮次即可使用该技能。

<details>
<summary>手动安装备用方法</summary>

将仓库中的 `save-papers-to-zotero` 目录复制到 Codex 技能目录：

```text
$CODEX_HOME/skills/save-papers-to-zotero
```

如果没有设置 `CODEX_HOME`，默认位置为 `~/.codex/skills/save-papers-to-zotero`。复制后重启 Codex 或开始新的对话轮次。

</details>

#### Claude Code

先将本仓库添加为 marketplace，再安装插件：

```text
/plugin marketplace add AlexXPZhu/save-papers-to-zotero
/plugin install save-papers-to-zotero@save-papers-to-zotero
```

Claude Code 可以自动调用该技能，也可以通过 `/save-papers-to-zotero:save-papers-to-zotero` 显式调用。需要获取仓库更新时运行：

```text
/plugin marketplace update save-papers-to-zotero
/plugin update save-papers-to-zotero@save-papers-to-zotero
```

卸载时运行 `/plugin uninstall save-papers-to-zotero@save-papers-to-zotero`。

### 3. 准备 Zotero

1. 启动 Zotero 桌面版。
2. 创建或确定目标集合。
3. 在请求中使用集合的准确名称。如果名称存在歧义，请使用 `--target-id` 提供集合键。

### 4. 让 Codex 或 Claude Code 导入论文

```text
把 https://arxiv.org/abs/1706.03762 保存到我的 Zotero 集合“待读论文”。
添加 to-read 标签，并将 arXiv Comments 保存为子笔记。
```

## 使用示例

### 单篇论文

```text
将 DOI 10.1145/3290605.3300233 导入准确的 Zotero 集合“HCI”。
保存并验证 PDF，并使用 reading 工作流标签。
```

### 可恢复的批量导入

```text
将 manifest.json 中的所有论文串行导入“论文资料”集合。
维护可恢复的账本，并报告可能重复项，但不要删除任何条目。
```

清单格式和恢复规则请参阅[批量清单参考](save-papers-to-zotero/references/batch-manifest.md)。

### 先执行试运行

```text
试运行这篇论文到“待读论文”集合的导入。
显示解析后的集合、元数据、标签、PDF 来源和可能重复项，但不要写入 Zotero。
```

### 需要浏览器登录会话的 PDF

```text
将这篇出版商论文保存到“待读论文”。我已在 Chrome 中拥有合法访问权限；
仅使用浏览器会话获取 PDF，不要导出 Cookie 或会话存储。
```

本技能不会绕过付费墙、CAPTCHA 或任何访问控制。

在 Claude Code 中，使用 `claude --chrome` 启动 CLI，或通过 `/chrome` 默认启用浏览器集成。浏览器会将有权访问的 PDF 下载到临时本地文件，再由导入器通过 `--pdf-file` 接收。如果 Chrome 不可用或下载失败，流程会在写入前停止，并要求你提供合法取得的本地 PDF。

## 工作流标签

本技能旨在与 [Ethereal Style](https://github.com/MuiseDestiny/zotero-style) Zotero 插件配套使用。技能会写入普通的 Zotero 标签，并采用 Ethereal Style 能识别的命名约定；随后插件可以将这些标签用于阅读状态和优先级工作流。即使没有安装插件，Zotero 也能保存这些标签，但安装 Ethereal Style 才能获得预期的完整集成体验。

| 请求的状态 | Zotero 标签 |
| --- | --- |
| `to-read`（默认） | `#status/to-read` |
| `reading` | `#status/reading` |
| `none` | 不添加状态标签 |

优先级是可选项，且必须由用户明确指定：`high`、`medium`、`low` 分别映射到 `#priority/high`、`#priority/medium`、`#priority/low`。本技能绝不会根据论文内容推断优先级。

## 安全与行为保证

| 保证 | 行为 |
| --- | --- |
| 准确目标 | 在预检阶段解析集合，并在实际写入前立即再次核验 |
| 非破坏性重复项处理 | 报告可能重复项；绝不删除、合并、替换现有条目，也不会因此静默取消用户要求的新建条目 |
| PDF 已验证 | 只有当存储附件可成功下载且文件内容确实是 PDF 时，才会报告 PDF 保存成功 |
| 诚实的失败状态 | 如果写入后的最终验证失败，结果会明确说明，并提醒条目可能已经存在 |
| 可恢复的批量任务 | 使用账本和锁串行导入；已完成的条目可被安全跳过 |
| 访问边界 | 不会破解付费墙、CAPTCHA、身份验证或其他访问控制 |
| 会话隐私 | 浏览器辅助获取不得导出 Cookie 或会话存储 |

## 结果状态

| 状态 | 含义 |
| --- | --- |
| `saved_with_pdf` | 父条目和存储的 PDF 均已创建并验证 |
| `ready` | 试运行预检完成，没有写入 Zotero |
| `skipped_completed` | 账本中该条目已完成 |
| `skipped_duplicate_in_manifest` | 同一批次中的重复记录被跳过 |
| `verification_failed` | 已发生写入，但最终验证未通过；重试前请先检查 Zotero |
| `not_attempted` | 必要前提未满足，因此没有尝试导入 |
| `invalid_pdf` / `http_error` | PDF 获取或验证失败 |

所有脚本均输出结构化 JSON，使调用者能够区分成功、跳过、预检失败和写入后状态不确定等情况。

## 故障排查

| 症状或代码 | 检查内容 |
| --- | --- |
| 无法连接 Zotero | 启动 Zotero 桌面版，并确认本地 Connector 服务可用 |
| `403` 或 `local_api_disabled` | 在 Zotero 设置中启用 **允许此计算机上的其他应用程序与 Zotero 通信** |
| `target_not_found` | 检查集合名称，并使用完全一致的拼写 |
| `target_ambiguous` | 使用 `--target-id` 提供目标集合键 |
| `invalid_pdf` | 确保来源是当前访问环境可直接获取的有效 PDF |
| `verification_failed` | 条目可能已经创建；不要盲目重试，请先检查 Zotero |
| Claude 浏览器不可用 | 使用 `claude --chrome` 启动并检查 `/chrome`；登录态浏览器获取要求 Anthropic 直连方案 |
| 浏览器下载失败 | 自行下载有权访问的 PDF 并提供本地路径；技能不会切换到不可信镜像 |

## 兼容性与测试

测试套件使用模拟 Zotero 服务，覆盖元数据、附件、验证、重复项、可恢复批量行为和双平台打包。GitHub Actions 会在 Windows、Linux 和 macOS 上使用 Python 3.10 与 3.12 运行测试，并使用 Claude Code 2.1.211 校验 Claude marketplace。

当前版本也已于 2026 年 7 月 18 日在 Zotero 9.0.6 和 Connector API v3 上完成实际验证。

```powershell
python -X utf8 -m unittest discover -s tests -v
```

## 仓库结构

```text
save-papers-to-zotero/
├── .claude-plugin/marketplace.json
├── .github/workflows/tests.yml
├── save-papers-to-zotero/
│   ├── SKILL.md
│   ├── agents/openai.yaml
│   ├── references/batch-manifest.md
│   └── scripts/
├── tests/
│   ├── test_importers.py
│   └── test_skill_packaging.py
├── README.md
├── README.zh-CN.md
└── LICENSE
```

## 开发与隐私

欢迎提交贡献和具体的问题报告。请将真实 PDF、清单、账本和其他私密研究数据放在仓库之外，或放入已忽略的 `local-data/` 目录。导入流程通过 Zotero 的本地回环 Connector 服务通信，且不应持久化浏览器 Cookie 或会话存储。

若要从仓库检出目录直接测试 Claude 插件，请运行 `claude --plugin-dir ./save-papers-to-zotero --chrome`；修改插件文件后使用 `/reload-plugins`。发布前从仓库根目录运行 `claude plugin validate .`。

## 许可证

本项目采用 [MIT License](LICENSE)。
