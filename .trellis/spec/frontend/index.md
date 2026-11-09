# Frontend Development Guidelines

> Best practices for frontend development in this project.

---

## Overview

The implemented UI uses Python NiceGUI, not React. Start with the code-backed NiceGUI contract below. Other entries remain bootstrap placeholders until their layer is implemented.

## Pre-Development Checklist

- Read [NiceGUI Workbench](./nicegui-workbench.md) before changing UI state, theme, charts or demo data.
- Read the active task PRD/design/implementation plan and the shared thinking guides.

## Quality Check

Run the lint, format, type and test commands in `nicegui-workbench.md`, then verify core changes in the in-app browser. Do not claim screenshot-only or HTTP-only checks establish interaction correctness.

---

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [NiceGUI Workbench](./nicegui-workbench.md) | Client state, themes, demo contracts and validation | Implemented |
| [功率与温度专题](./power-temperature.md) | 真实测点、快照状态、双图联动与只读源 | Implemented |
| [AHU回风温度专题](./ahu-temperature.md) | 条件恢复、窗口摘要、五台对比与异步快照 | Implemented |
| [每日用电占比](./energy-share.md) | 每日用电页面与快照 | Implemented |
| [算法流程图](./algorithm-flowcharts.md) | 草稿／预览／保存版本、画布与切换保护 | 已实现并验证 |
| [Directory Structure](./directory-structure.md) | Module organization and file layout | To fill |
| [Component Guidelines](./component-guidelines.md) | Component patterns, props, composition | To fill |
| [Hook Guidelines](./hook-guidelines.md) | Custom hooks, data fetching patterns | To fill |
| [State Management](./state-management.md) | Local state, global state, server state | To fill |
| [Quality Guidelines](./quality-guidelines.md) | Code standards, forbidden patterns | To fill |
| [Type Safety](./type-safety.md) | Type patterns, validation | To fill |

---

## How to Fill These Guidelines

For each guideline file:

1. Document your project's **actual conventions** (not ideals)
2. Include **code examples** from your codebase
3. List **forbidden patterns** and why
4. Add **common mistakes** your team has made

The goal is to help AI assistants and new team members understand how YOUR project works.

---

**语言**：AI 约束、开发规范、代码注释和文档字符串以中文为主；必要的技术标识保留英文。Git 提交格式遵循根目录 `AGENTS.md`：按变更选择类型（如`feat`、`fix`、`docs`），后接中文说明，如 `fix: 修复styles.css文件的暗色样式`。
