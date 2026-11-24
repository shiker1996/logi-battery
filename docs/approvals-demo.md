# 审批门禁 demo

这个分支用于验证一条链路：**策略文件 → 检查 → 必需状态 → 阻断合并**。

## 文件

| 文件 | 作用 |
| --- | --- |
| `.approvals.yml` | 策略：按路径规定一次变更需要几个人批准 |
| `scripts/approvals_check.py` | 判定脚本：读策略、数批准人数、把结论写成 commit status |
| `.github/workflows/approvals.yml` | 在 PR 事件上触发上面那个脚本 |

## 策略语义

- `paths`：文件路径模式，`**` 表示任意层级
- `seats`：该路径下变更需要的最少批准人数，作者本人不计入
- 匹配到多条规则时取 `seats` 的最大值
- 没有匹配到任何规则时放行（否则必需检查会因为永远没有结果而把 PR 卡死）

## 当前规则

1. `**` → 1 人
2. `.approvals.yml`、`scripts/approvals_check.py`、`.github/workflows/approvals.yml` → 2 人

第二条是刻意的：改门禁规则这件事本身需要更高的门槛，否则谁都能顺手把规则改松。

## 已知边界

- 这个 demo 不含任何 AI 判断，只有确定性的规则计算
- 作者不能批准自己的 PR，单账号环境下无法演示「通过」
- commit status 名称固定为 `approvals-policy`
