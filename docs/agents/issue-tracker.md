# Issue tracker: GitHub

本仓库的工单和实施规格存放在 GitHub Issues，使用 `gh` CLI。操作前根据用户指定的目标和 Git remote 核对仓库；fork 场景必须区分当前仓库与上游。所有写操作显式指定目标仓库，不依赖 `gh` 的默认仓库选择。

技能要求“发布工单”时创建 GitHub issue，要求“读取工单”时读取其完整正文与评论。发布前确认已有授权，已有会话授权继续有效；本约定不扩大提交、推送、合并或部署权限。

## 基本操作

下列命令中的 `<owner>/<repo>` 替换为已核对的目标仓库。多行正文写入临时 UTF-8 文件后传给 `--body-file`，不将正文拼接成 shell 代码。

- 创建：`gh issue create --repo <owner>/<repo> --title "..." --body-file <正文文件> --label <标签>`。
- 读取：`gh issue view --repo <owner>/<repo> <编号> --json number,title,body,labels,comments,state`。
- 列表：`gh issue list --repo <owner>/<repo> --state open --json number,title,labels`，按需添加标签和状态过滤。
- 更新正文：`gh issue edit --repo <owner>/<repo> <编号> --body-file <正文文件>`。
- 评论：`gh issue comment --repo <owner>/<repo> <编号> --body-file <正文文件>`。
- 标签：`gh issue edit --repo <owner>/<repo> <编号> --add-label <标签>` 或 `--remove-label <标签>`；分流标签含义见 `triage-labels.md`。
- 关闭：验收完成且获得相应授权后使用 `gh issue close --repo <owner>/<repo> <编号>`。PR/CI 状态不能单独代替工单验收。

GitHub 的 issue 和 PR 共用编号空间。引用编号时核对类型；读取失败先判断权限、网络或对象类型，不将错误一律当成 issue 不存在。

## 父子工单与阻塞关系

先创建阻塞项，再创建被阻塞项，使引用使用真实编号。拆分既有 issue 时，将工单关联为 sub-issue，不因此关闭或改写父工单正文。

以下操作使用 GitHub REST API，不依赖 CLI 的子工单参数。命令中的仓库和 ID 应由当前目标及查询结果取得。

- 查询数据库 ID：`gh api repos/<owner>/<repo>/issues/<编号> --jq .id`。数据库 ID 与 issue 编号、node_id 不同。
- 添加子工单：`gh api --method POST repos/<owner>/<repo>/issues/<父编号>/sub_issues -F sub_issue_id=<子工单数据库ID>`。
- 添加阻塞关系：`gh api --method POST repos/<owner>/<repo>/issues/<被阻塞编号>/dependencies/blocked_by -F issue_id=<阻塞项数据库ID>`。
- 检查关系：使用相同关系路径的 GET，并在重复执行前确认关联不存在，避免重复写入。

优先使用 GitHub 原生关系。确认平台不支持时，在子工单正文记录 `Part of #<父编号>` 和 `Blocked by: #<阻塞编号>`；鉴权或网络失败不算平台不支持。除另有授权，不为兼容回退改写父工单正文。

施工只领取阻塞项已完成验收的工单。`ready-for-agent` 表示规格可施工，不覆盖阻塞关系；某个阻塞项仅被关闭而未交付验收时，不能自动解锁。无阻塞关系的工单可以独立推进，共用文件冲突通过协作处理。

## Pull requests as a triage surface

**PRs as a request surface: no.**

## 证据与本地产物

GitHub 工单是工作状态真源。本地草稿、原始调研和临时验收材料遵守仓库现有产物约定；不要维护一份相互竞争的本地工单状态。发布仅包含必要的脱敏证据，不上传密钥、完整认证头或私人推理文本。
