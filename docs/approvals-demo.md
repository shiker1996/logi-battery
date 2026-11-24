# 审批门禁 demo

验证一条链路：**策略文件 → 检查 → 必需状态 → 阻断合并**，以及 **AI 能不能成为那个批准人**。

## 文件

| 文件 | 作用 |
| --- | --- |
| `.approvals.yml` | 策略：按路径规定一次变更需要几个人批准，以及什么条件下可以抵消 |
| `scripts/approvals_check.py` | 判定脚本：读策略、数批准人数、把结论写成 commit status |
| `.github/workflows/approvals.yml` | 在 PR 事件上触发上面那个脚本 |

## 策略语义

- `paths`：文件路径模式，`**` 表示任意层级
- `seats`：该路径下变更需要的最少批准人数，作者本人不计入
- `override`：条件满足时降低门槛，目前只支持「某个 commit status 是否通过」
- 匹配到多条规则时，取最终 `seats` 的最大值
- 没有匹配到任何规则时放行（否则必需检查会因为没有结果而把 PR 卡死）

## 当前规则

1. `**` → 1 人；若 `ai-review` 状态为 success，降为 **0 人**（AI 代批）
2. `.approvals.yml`、`scripts/approvals_check.py`、`.github/workflows/approvals.yml` → 2 人，**AI 不适用**

第二条是刻意留的护栏：AI 可以批准别人的代码，但不能给自己放权改规则。

## AI 那一步怎么来的

真实部署里，AI 审查应该在 Actions 里跑：

1. 拉 PR 的 diff
2. 调模型，要一个 PASS / FAIL 结论
3. 把结论 POST 成 `ai-review` 这个 commit status
4. 策略里的 `override` 再去读它

这个 demo 为了不往仓库里塞模型密钥，AI 那一步是在本地跑的（Claude Code + 第三方模型网关），结论同样以 commit status 的形式推到 PR 上。**协议完全一样**，只是执行位置不同——换成 Actions 只要把命令搬过去、把 key 放进 repo secret。

## 三段演示

| 幕 | 场景 | 预期 |
| --- | --- | --- |
| 1 | 普通改动，没有 `ai-review` 状态 | 还差 1 人，BLOCKED |
| 2 | 补上 `ai-review` = success | AI 代批，PASS，可合并 |
| 3 | 改动 `.approvals.yml` 本身 | 仍需 2 人，BLOCKED（AI 无效） |

## 已知边界

- 作者不能批准自己的 PR，所以单账号环境下只能靠 AI 那一票放行
- commit status 名称：`approvals-policy`（本检查）、`ai-review`（AI 结论）
- 没有实现观察模式（Claude Ship 里的 `mode: observe`）
