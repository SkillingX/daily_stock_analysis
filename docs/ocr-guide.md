# open-code-review (ocr) 使用指南

open-code-review（简称 ocr）是阿里巴巴开源的 AI 代码审查 CLI 工具，基于 diff 上下文进行精度优化，token 消耗约为通用 agent 的 1/9。

## 目录

1. [安装与配置](#1-安装与配置)
2. [核心命令](#2-核心命令)
3. [本地开发流程](#3-本地开发流程)
4. [CI 集成](#4-ci-集成)
5. [Loop Engineering 集成](#5-loop-engineering-集成)
6. [配置参考](#6-配置参考)
7. [常见问题](#7-常见问题)

---

## 1. 安装与配置

### 1.1 安装

```bash
npm install -g @alibaba-group/open-code-review
```

验证安装：

```bash
ocr --version
# open-code-review v1.9.1 (200424c) darwin/arm64
```

### 1.2 LLM Provider 配置

ocr 支持 20+ 内置 provider，按协议分为两类：

**Anthropic 系列**

| Provider | 说明 |
|---|---|
| `anthropic` | 官方 Anthropic API（推荐，支持 claude 系列模型） |

**OpenAI 兼容系列**

| Provider | 说明 |
|---|---|
| `openai` | OpenAI 官方 |
| `deepseek` | DeepSeek |
| `dashscope` | 阿里云 DashScope |
| `kimi` | 月之暗面 Kimi |
| `minimax` / `minimax-cn` | MiniMax |
| `z-ai` / `z-ai-coding` | 智谱 BigModel |
| `volcengine` | 火山引擎 |
| `baidu-qianfan` | 百度千帆 |
| `ollama-cloud` | Ollama Cloud |
| `litellm` | 本地 LiteLLM 代理 |

**交互式配置（推荐新手）**

```bash
ocr config provider          # 选择 provider
ocr config model             # 选择模型
ocr config set providers.anthropic.api_key "$ANTHROPIC_API_KEY"
```

**非交互式配置（CI / 自动化）**

```bash
# 使用 Anthropic
ocr config set provider anthropic
ocr config set providers.anthropic.api_key "$ANTHROPIC_API_KEY"
ocr config set model claude-opus-4-6

# 使用 DeepSeek（便宜）
ocr config set provider deepseek
ocr config set providers.deepseek.api_key "$DEEPSEEK_API_KEY"
ocr config set model deepseek-chat

# 使用自定义 LiteLLM 代理
ocr config set custom_providers.local-gateway.url http://localhost:4000/v1
ocr config set custom_providers.local-gateway.protocol openai
ocr config set provider local-gateway
```

### 1.3 验证配置

```bash
ocr llm test
```

输出示例：

```
✓ Provider: anthropic
✓ Model: claude-opus-4-6
✓ Connection: OK
```

### 1.4 配置存储位置

- 用户级配置：`~/.opencodereview/config.json`
- Session 历史：`~/.opencodereview/sessions/`
- 更新检查：`~/.opencodereview/update-available`

---

## 2. 核心命令

### 2.1 `ocr review` — Diff 审查（主要工作流）

对 git diff 进行 AI 审查，支持多种范围指定方式。

**基本用法**

```bash
# 审查当前 workspace 的 staged + unstaged 变更
ocr review --repo .

# 审查两个分支之间的差异
ocr review --from main --to feature-branch

# 审查单个 commit
ocr review --commit abc123

# 审查指定文件列表
ocr review --from main --to HEAD --path src/pages/HomePage.tsx,src/api/stock.ts
```

**关键参数**

| 参数 | 说明 | 示例 |
|---|---|---|
| `--from` / `--to` | 指定分支或 commit 范围 | `--from main --to feature` |
| `--commit` / `-c` | 指定单个 commit | `--commit abc123` |
| `--repo` | git 根目录（默认 cwd） | `--repo .` |
| `--format` | 输出格式：`text`（默认）或 `json` | `--format json` |
| `--audience` | 受众：`human`（完整报告）或 `agent`（摘要） | `--audience human` |
| `--concurrency` | 最大并发文件审查数（默认 8） | `--concurrency 4` |
| `--max-tokens-budget` | 单次运行总 token 上限 | `--max-tokens-budget 8000` |
| `--max-tools` | 每个文件最大 tool 调用轮次 | `--max-tools 20` |
| `--timeout` | 单个任务超时（分钟，默认 10） | `--timeout 15` |
| `--exclude` | gitignore 风格排除模式 | `--exclude "node_modules/**,*.log"` |
| `--preview` / `-p` | 仅预览文件列表，不跑 LLM | `--preview` |
| `--resume <session-id>` | 恢复历史 review session | `--resume <uuid>` |

**输出到 PR Comment（GitHub）**

```bash
ocr review \
  --from origin/${{ github.base_ref }} \
  --to HEAD \
  --repo . \
  --format text \
  --audience human \
  --concurrency 4 \
  --max-tokens-budget 8000 \
  2>&1 | gh pr comment $PR_NUMBER --body-file -
```

### 2.2 `ocr scan` — 全文审计

对指定文件或目录进行完整审查，无需 git diff。

**基本用法**

```bash
# 审计整个仓库
ocr scan

# 审计指定文件
ocr scan --path src/pages/HomePage.tsx

# 审计多个文件（逗号分隔）
ocr scan --path src/pages/HomePage.tsx,src/api/stock.ts

# 审计目录
ocr scan --path apps/dsa-web/src/components

# 预览（不跑 LLM）
ocr scan --path src/ --preview
```

**关键参数**

| 参数 | 说明 |
|---|---|
| `--path` | 目标文件或目录（逗号分隔） |
| `--batch` | 批处理策略：`none`（默认）、`by-language`、`by-directory` |
| `--no-plan` | 跳过 PLAN_TASK 预检阶段 |
| `--no-dedup` | 跳过 DEDUP_TASK 去重阶段 |
| `--no-summary` | 跳过 PROJECT_SUMMARY_TASK 汇总阶段 |

**组合使用：排除噪音文件**

```bash
ocr scan \
  --path . \
  --exclude "node_modules/**,*.log,*.min.js,dist/**,build/**" \
  --no-summary
```

### 2.3 `ocr delegate` — 委托模式

输出 review spec（文件列表 + 解析后的规则），交给 host agent（如 Claude Code）执行 LLM 调用。ocr 只负责确定性的文件选择和规则解析。

**基本用法**

```bash
# 预览可审查的文件及模式/引用元数据
ocr delegate preview --from main --to feature

# 预览当前 workspace
ocr delegate preview

# 输出指定文件的解析后规则
ocr delegate rule src/main.go src/handler.go
```

**使用场景**：当你希望 Claude Code 执行实际的 LLM 审查，但由 ocr 决定审哪些文件、按什么规则审。

### 2.4 `ocr session` — Session 管理

Review session 会持久化到 `~/.opencodereview/sessions/<encoded-repo>/<uuid>.jsonl`，可 replay。

```bash
# 列出最近 sessions
ocr session list
ocr session list --limit 10 --json

# 查看 session 详情
ocr session show <session-id>

# 查看 session 中的评论
ocr session comments <session-id>
```

### 2.5 `ocr viewer` — WebUI

启动本地 WebUI 查看 review session。

```bash
ocr viewer                  # 默认 localhost:5483
ocr viewer --addr :3000    # 绑定到指定端口
```

### 2.6 `ocr config` — 配置管理

```bash
ocr config provider              # 交互式选择 provider
ocr config model                 # 交互式选择模型
ocr config set provider <name>   # 非交互式设置 provider
ocr config set model <model>     # 非交互式设置模型
ocr config set providers.<name>.api_key "$API_KEY"
ocr config set custom_providers.my-gateway.url https://...   # 添加自定义 provider
ocr config set custom_providers.my-gateway.protocol openai
ocr config unset mcp_servers.github                          # 取消 MCP 集成
```

---

## 3. 本地开发流程

### 3.1 Pre-commit Review（推荐）

在 `git commit` 前跑 ocr review，尽早发现问题：

```bash
# 审查当前所有未提交变更
ocr review --repo . --format text --audience human
```

如果发现严重问题，修改后重新审查，直到通过再提交。

### 3.2 指定文件快速审查

只审查改动的文件，避免全量审查浪费时间：

```bash
# 获取最近改动的文件
git diff --name-only main...HEAD

# 审查指定文件
ocr scan --path src/pages/HomePage.tsx,apps/dsa-web/src/api/stock.ts
```

### 3.3 分支对比审查

```bash
# 对比 feature 分支与 main
ocr review --from main --to feature-branch --format text --audience human
```

### 3.4 恢复历史 Review

```bash
# 列出历史 sessions
ocr session list

# 恢复指定 session
ocr session show <session-id>
```

---

## 4. CI 集成

### 4.1 GitHub Actions 示例

```yaml
name: OCR Review

on:
  pull_request_target:
    types: [opened, synchronize, reopened]

jobs:
  ocr-review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          ref: ${{ github.event.pull_request.head.sha }}
          fetch-depth: 0

      - name: Install OCR
        run: npm install -g @alibaba-group/open-code-review

      - name: Fetch base branch
        run: git fetch origin ${{ github.base_ref }}:refs/remotes/origin/${{ github.base_ref }}

      - name: Run OCR review
        env:
          OCR_NO_UPDATE: 1
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          ocr review \
            --from origin/${{ github.base_ref }} \
            --to HEAD \
            --repo . \
            --format text \
            --audience human \
            --concurrency 4 \
            --max-tokens-budget 8000 \
            2>&1 | gh pr comment ${{ github.event.pull_request.number }} --body-file -
```

### 4.2 CI 环境注意事项

1. **`OCR_NO_UPDATE=1`**：禁用 self-update 检查，避免 CI 中不必要的延迟
2. **Token Budget**：CI 环境中设置 `--max-tokens-budget` 控制最大消耗
3. **Concurrency**：默认 8 并发，CI 中建议降至 4 避免 API 限流
4. **Exit Code**：当所有文件都失败时 ocr 才会非零退出；部分失败仍会输出结果并 exit 0

### 4.3 与现有 CI 流程结合

ocr 可以替代或增强现有的 `ai_review.py` 步骤：

```yaml
# pr-review.yml 中的 ai-review job（已集成）
- name: OCR AI Review
  env:
    OCR_NO_UPDATE: 1
  run: |
    ocr review \
      --from origin/${{ github.base_ref }} \
      --to HEAD \
      --repo . \
      --format text \
      --audience human \
      --concurrency 4 \
      --max-tokens-budget 8000 \
      2>&1 | gh pr comment $PR_NUMBER --body-file -
```

---

## 5. Loop Engineering 集成

### 5.1 三层集成架构

| 层级 | 工具 | 作用 |
|---|---|---|
| 本地预审 | `/ocr-review` skill | commit 前 catch 问题 |
| CI 审查 | `pr-review.yml` ocr job | PR 创建时自动 review |
| Loop 追踪 | `loop-ci-sweeper.yml` ocr-scan job | CI 失败时定向审计 |

### 5.2 本地 Skill

Claude Code skill 封装了 ocr 常用操作，位于 `.claude/skills/ocr-review/SKILL.md`。

**触发方式**：Claude Code 中直接使用 skill 命令

### 5.3 每日 Review（loop-triage）

`loop-triage.yml` 中的 `ocr-review` job 对近 7 天变更文件做日常扫描：

```yaml
- name: Run OCR review on recent changes
  env:
    OCR_NO_UPDATE: 1
  run: |
    CHANGED_FILES=$(git diff --name-only --since="7 days ago" --all | grep -vE "^(node_modules|\.git|loop-run-log|loop-budget|STATE\.md)$" | head -20)
    ocr scan --path "$CHANGED_LIST" --format text --audience human --concurrency 4
```

### 5.4 CI 失败定向审计（loop-ci-sweeper）

CI 失败时，`loop-ci-sweeper.yml` 中的 `ocr-scan` job 对失败 PR 的变更文件定向审计：

```yaml
- name: Run OCR scan on changed files
  env:
    OCR_NO_UPDATE: 1
  run: |
    CHANGED_FILES=$(gh pr view "$PR_NUMBER" --json files --jq '.files[].path' | tr '\n' ',')
    ocr scan --path "$CHANGED_FILES" --format text --no-summary --audience human
```

---

## 6. 配置参考

### 6.1 完整 config.json 示例

```json
{
  "provider": "anthropic",
  "model": "claude-opus-4-6",
  "providers": {
    "anthropic": {
      "api_key": "$ANTHROPIC_API_KEY"
    },
    "deepseek": {
      "api_key": "$DEEPSEEK_API_KEY"
    },
    "local-gateway": {
      "url": "http://localhost:4000/v1",
      "protocol": "openai"
    }
  },
  "review": {
    "concurrency": 4,
    "max_tokens_budget": 8000,
    "timeout": 10
  },
  "scan": {
    "batch": "by-directory",
    "no_plan": false,
    "no_dedup": false,
    "no_summary": false
  }
}
```

### 6.2 环境变量

| 变量 | 说明 | 推荐值 |
|---|---|---|
| `OCR_NO_UPDATE` | 禁用 self-update 检查 | `1`（CI 中必设） |
| `OCR_VERSION` | 锁定版本 | `v1.9.1` |
| `ANTHROPIC_API_KEY` | Anthropic API Key | 通过凭据管理器获取 |
| `DEEPSEEK_API_KEY` | DeepSeek API Key | 通过凭据管理器获取 |

### 6.3 内置 Review Rules

ocr 内置了对以下场景的专项审查规则：

**GitHub Actions 规则**（`.github/workflows/*.yml`）
- 错误的 `if:` 事件名（`pull_request` vs `pull_request_target`）
- `run:` 块中的脚本注入风险（`${{ github.event.* }}`）
- 断裂的 `needs:` 依赖 / 循环依赖
- 硬编码凭据
- 缺少 `fetch-depth: 0`
- 缺少 concurrency 控制
- 依赖未缓存

**通用规则**
- 安全敏感文件修改检测
- 配置文件变更风险评估

### 6.4 自定义 Rules

通过 `--rule <json>` 指定自定义规则文件：

```json
{
  "rules": [
    {
      "path": "src/**/*.ts",
      "content": "禁止使用 any 类型",
      "severity": "error"
    }
  ]
}
```

---

## 7. 常见问题

### Q1: ocr 和通用 AI agent 做 code review 的区别？

| 维度 | ocr | 通用 AI Agent |
|---|---|---|
| Token 消耗 | ~1/9 | 全量上下文 |
| 精度（F1） | 高（benchmark 更高） | 中等 |
| 召回率 | 较低（设计优先精度） | 较高 |
| 速度 | 快（10x） | 慢 |
| 工具调用能力 | 有（多轮 tool use） | 有 |
| Git 上下文感知 | 原生 | 需要额外 prompt |
| 内置规则 | 有（GitHub Actions 等） | 无 |

### Q2: 报错 `Failed to fetch` 或连接超时

检查：
1. API Key 是否正确配置：`ocr llm test`
2. 网络是否能访问对应 API 端点
3. 代理设置（如需要）：`export HTTPS_PROXY=http://proxy:8080`

### Q3: 审查结果质量不如预期

尝试：
1. 调整 `--max-tokens-budget` 上限，增加 budget
2. 切换到更强大的模型（如 `claude-opus-4-6`）
3. 使用 `--max-tools` 增加单文件审查深度
4. 使用 `--audience human` 获得完整报告

### Q4: CI 中 self-update 检查导致超时

在 CI 环境中**必须**设置 `OCR_NO_UPDATE=1`：

```yaml
env:
  OCR_NO_UPDATE: 1
```

### Q5: 如何只审查特定类型的文件？

```bash
# 只审查 Python 文件
ocr scan --path $(git diff --name-only --diff-filter=M | grep '\.py$')

# 只审查新增的文件
ocr scan --path $(git diff --name-only --diff-filter=A)
```

### Q6: ocr 的 session 可以跨机器恢复吗？

session 文件在 `~/.opencodereview/sessions/`，可以复制到其他机器的同路径下，用 `ocr session show <id>` 恢复查看。

### Q7: `--from main --to HEAD` 和 `--from main` 有什么区别？

- `--from main --to HEAD`：审查 main 到 HEAD 之间的所有变更
- `--from main`：以 main 为 base，审查当前分支相对于 main 的变更（等同于 `--from main --to HEAD`）

---

## 附录：命令速查

```bash
# 安装
npm install -g @alibaba-group/open-code-review

# 配置
ocr config set provider anthropic
ocr config set providers.anthropic.api_key "$ANTHROPIC_API_KEY"
ocr config set model claude-opus-4-6

# 验证
ocr --version
ocr llm test

# Diff review
ocr review --from main --to feature-branch
ocr review --commit abc123
ocr review --repo . --preview

# Full-file scan
ocr scan --path src/pages/HomePage.tsx
ocr scan --path apps/dsa-web/src/components --exclude "node_modules/**"

# Delegate
ocr delegate preview --from main --to HEAD
ocr delegate rule src/main.go

# Session
ocr session list
ocr session show <session-id>
ocr viewer

# Help
ocr review --help
ocr scan --help
ocr config --help
```

## GitHub PR 工作流的权限边界

PR Review 的静态检查在 pull_request 下以 contents:read 执行，不导入 PR 的 Python 代码。pull_request_target 的标签/说明只使用 GitHub API，不检出 PR 代码，也不读取来自 PR 的产物执行指令。

自动 AI 审查复用仓库现有 ai_review 脚本：仅同仓库 PR 且仓库变量 ENABLE_AI_REVIEW=true 时启用，运行 base 提交的可信脚本，head 只用于 git diff，不检出或执行。需要既有 GEMINI_API_KEY 或 OPENAI_API_KEY 仓库 secret，模型与地址使用对应仓库变量；缺少配置或审查结果则失败，不能表示审查通过。fork AI 审查跳过，可由维护者本机使用 OCR 审查；首次贡献的 Actions 审批仍需维护者处理。审查结果作为带 PR 编号和提交 SHA 的 artifact 展示，不由有写权限作业执行或信任。

此文档为中文工具说明，无对应英文版本；贡献指南的通用流程未改动。
