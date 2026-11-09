# Backend Development Guidelines

> Best practices for backend development in this project.

---

## Overview

This directory contains guidelines for backend development. Fill in each file with your project's specific conventions.

---

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [航班只读契约](./flights.md) | SurrealDB字段、时间口径、空档计算与凭据边界 | Implemented |
| [功率与温度只读契约](./power-temperature.md) | 小麦认证、测点校验、历史网格、统计及导出 | Implemented |
| [AHU回风温度合同](./ahu-temperature.md) | 每日窗口、运行配对、分位数、质量与只读源 | Implemented |
| [每日用电占比](./energy-share.md) | 每日用电领域与数据源 | Implemented |
| [算法流程图](./algorithm-flowcharts.md) | 图示版本、项目隔离、SQLite与SVG边界 | 已实现并验证 |
| [登录与准入](./authentication.md) | 小麦身份、会话与HTTP／Socket边界 | 已实现 |
| [Directory Structure](./directory-structure.md) | Module organization and file layout | To fill |
| [Database Guidelines](./database-guidelines.md) | ORM patterns, queries, migrations | To fill |
| [Error Handling](./error-handling.md) | Error types, handling strategies | To fill |
| [Quality Guidelines](./quality-guidelines.md) | Code standards, forbidden patterns | To fill |
| [Logging Guidelines](./logging-guidelines.md) | Structured logging, log levels | To fill |

---

## How to Fill These Guidelines

For each guideline file:

1. Document your project's **actual conventions** (not ideals)
2. Include **code examples** from your codebase
3. List **forbidden patterns** and why
4. Add **common mistakes** your team has made

The goal is to help AI assistants and new team members understand how YOUR project works.

---

**语言**：AI 约束、开发规范、代码注释和文档字符串以中文为主；必要的技术标识保留英文。Git 提交格式遵循根目录 `AGENTS.md`：按变更选择类型（如`feat`、`fix`、`docs`），后接中文说明，如 `fix: 修复demo.py文件的数据校验逻辑`。
