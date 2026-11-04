<!-- TRELLIS:START -->
# Trellis Instructions

These instructions are for AI assistants working in this project.

This project is managed by Trellis. The working knowledge you need lives under `.trellis/`:

- `.trellis/workflow.md` — development phases, when to create tasks, skill routing
- `.trellis/spec/` — package- and layer-scoped coding guidelines (read before writing code in a given layer)
- `.trellis/workspace/` — per-developer journals and session traces
- `.trellis/tasks/` — active and archived tasks (PRDs, research, jsonl context)

If a Trellis command is available on your platform (e.g. `/trellis:finish-work`, `/trellis:continue`), prefer it over manual steps. Not every platform exposes every command.

If you're using Codex or another agent-capable tool, additional project-scoped helpers may live in:
- `.agents/skills/` — reusable Trellis skills
- `.codex/agents/` — optional custom subagents

Managed by Trellis. Edits outside this block are preserved; edits inside may be overwritten by a future `trellis update`.

<!-- TRELLIS:END -->

# 个人开发约定

以下约定适用于本项目的 AI 协作，优先于通用模板中的语言和提交信息示例。

## 中文优先

- AI 约束、项目规范、提示词及工作流说明以中文为主。
- 新增或修改的代码注释和文档字符串以中文为主，解释意图、边界及原因。
- 文件名、标识符、接口字段、命令和第三方原文保留必要英文。
- 不为翻译而无关重写第三方或工具生成内容；项目后续自行维护的约束、规范及注释使用中文。

## 提交信息

- Git 提交类型根据实际变更选择，如 `feat`（新增功能）、`fix`（修复问题）、`docs`（文档）、`refactor`（重构）、`test`（测试）、`chore`（维护）；不固定使用 `fix`。
- 格式：`<type>: <中文说明>`；冒号后留一个空格，说明中的动词与实际变更一致，不固定使用“修复”。
- 示例：`feat: 新增workbench.py文件的主题切换功能`、`fix: 修复workbench.py文件的主题切换逻辑`、`docs: 更新AGENTS.md文件的提交约定`。
- 多文件示例：`refactor: 重构workbench.py、styles.css文件的主题样式组织`。
- 文件较多时可以明确的模块或目录概括，说明须与实际变更一致。
- 自动提交同样遵守此格式；工具默认格式不符合时，改用可控制提交信息的方式。
