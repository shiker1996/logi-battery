#!/usr/bin/env python3
"""策略驱动的批准检查（含 AI 抵消）。

流程：
  1. 读仓库里的 .approvals.yml（策略）
  2. 拉这个 PR 改了哪些文件
  3. 按路径匹配规则，得到基准 seats
  4. 若规则带 override 且对应 commit status 已通过，则用 override 的 seats
  5. 拉 PR 评审记录，数出有效批准人数
  6. 把结论写成 commit status（名字 approvals-policy）
  7. 不满足则退出码 1

环境变量：
  GITHUB_TOKEN / REPO / PR_NUMBER / HEAD_SHA
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
STATUS_NAME = "approvals-policy"
POLICY_PATH = ".approvals.yml"


# ---------- GitHub API ----------

def _open(path: str, method: str = "GET", body: dict | None = None):
    req = urllib.request.Request(f"{API}{path}", method=method)
    req.add_header("Authorization", f"Bearer {os.environ['GITHUB_TOKEN']}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "approvals-check")
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8") or "null")


def gh_list(path: str) -> list:
    items: list = []
    page = 1
    while True:
        sep = "&" if "?" in path else "?"
        with_one = _open(f"{path}{sep}per_page=100&page={page}")
        if not isinstance(with_one, list) or not with_one:
            break
        items.extend(with_one)
        if len(with_one) < 100:
            break
        page += 1
    return items


def gh_one(path: str) -> dict:
    out = _open(path)
    return out if isinstance(out, dict) else {}


def post_status(state: str, description: str) -> None:
    _open(
        f"/repos/{os.environ['REPO']}/statuses/{os.environ['HEAD_SHA']}",
        method="POST",
        body={"state": state, "context": STATUS_NAME, "description": description[:140]},
    )


# ---------- 策略解析 ----------

def load_policy() -> dict:
    """解析本 demo 用到的那一小撮 YAML，避免引入依赖。

    生产环境请换 pyyaml；这里刻意保持零依赖，方便单文件复制。
    """
    if not os.path.exists(POLICY_PATH):
        return {"rules": []}

    rules: list[dict] = []
    cur: dict | None = None
    override: dict | None = None
    mode = "none"  # none | paths | override

    with open(POLICY_PATH, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].rstrip()
            if not line.strip():
                continue
            text = line.strip()

            if text.startswith("- name:"):
                cur = {"name": text.split(":", 1)[1].strip(), "paths": [],
                       "seats": 1, "override": []}
                rules.append(cur)
                override, mode = None, "none"
            elif cur is None:
                continue
            elif text == "paths:":
                mode = "paths"
            elif text == "override:":
                mode = "override"
            elif mode == "override" and text.startswith("- when_status:"):
                override = {"when_status": text.split(":", 1)[1].strip(),
                            "seats": 0}
                cur["override"].append(override)
            elif text.startswith("seats:"):
                value = int(text.split(":", 1)[1].strip())
                if mode == "override" and override is not None:
                    override["seats"] = value
                else:
                    cur["seats"] = value
                    mode = "none"
            elif mode == "paths" and text.startswith("- "):
                cur["paths"].append(text[2:].strip().strip('"').strip("'"))
    return {"rules": rules}


def glob_to_regex(pattern: str) -> re.Pattern:
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            if i < len(pattern) and pattern[i] == "/":
                i += 1
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def effective_seats(rule: dict, changed: list[str], statuses: dict[str, str]) -> int:
    pats = [glob_to_regex(p) for p in rule["paths"]]
    if not any(p.match(f) for p in pats for f in changed):
        return -1  # 不匹配

    seats = rule["seats"]
    for ov in rule["override"]:
        if statuses.get(ov["when_status"]) == "success" and ov["seats"] < seats:
            seats = ov["seats"]
    return seats


# ---------- 评审 ----------

def latest_statuses(sha: str) -> dict[str, str]:
    """每个 context 只取最新一次状态。"""
    out: dict[str, str] = {}
    for item in reversed(gh_list(f"/repos/{os.environ['REPO']}/commits/{sha}/statuses")):
        ctx = item.get("context", "")
        if ctx and ctx not in out:
            out[ctx] = item.get("state", "")
    return out


def count_approvals(reviews: list, author: str) -> tuple[int, list[str]]:
    latest: dict[str, str] = {}
    for rev in reviews:
        user = (rev.get("user") or {}).get("login", "")
        if not user or user == author:
            continue
        if rev.get("user", {}).get("type") == "Bot" or user.endswith("[bot]"):
            continue
        state = rev.get("state", "")
        if state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[user] = state
    approved = sorted(u for u, s in latest.items() if s == "APPROVED")
    return len(approved), approved


# ---------- 主流程 ----------

def main() -> int:
    repo = os.environ["REPO"]
    pr = os.environ["PR_NUMBER"]
    sha = os.environ["HEAD_SHA"]

    author = (gh_one(f"/repos/{repo}/pulls/{pr}").get("user") or {}).get("login", "")
    changed = [f["filename"] for f in gh_list(f"/repos/{repo}/pulls/{pr}/files")]
    policy = load_policy()
    statuses = latest_statuses(sha)
    have, who = count_approvals(gh_list(f"/repos/{repo}/pulls/{pr}/reviews"), author)

    matched: list[tuple[str, int]] = []
    for rule in policy["rules"]:
        seats = effective_seats(rule, changed, statuses)
        if seats >= 0:
            matched.append((rule["name"], seats))

    need, rule_name = (0, "-")
    for name, seats in matched:
        if seats > need:
            need, rule_name = seats, name

    ai = statuses.get("ai-review", "missing")
    print(f"PR #{pr} by {author} @ {sha[:7]}")
    print(f"changed files ({len(changed)}): {', '.join(changed)}")
    print(f"ai-review status: {ai}")
    print(f"matched rules: {matched or '-'}")
    print(f"seats required: {need} (rule={rule_name})")
    print(f"human approvals: {have} {who}")

    if need == 0:
        desc = "AI 审查通过，代 1 人批准（default 要求 1 人）" if ai == "success" else "无匹配规则，放行"
        print(f"PASS - {desc}")
        post_status("success", desc)
        return 0

    if have >= need:
        desc = f"批准 {have}/{need}，满足策略（{rule_name}）"
        print(f"PASS - {desc}")
        post_status("success", desc)
        return 0

    if ai == "success":
        desc = f"还差 {need} 人批准（{rule_name}：AI 不能抵消元变更）"
    else:
        desc = f"还差 {need} 人批准（{rule_name} 要求 {need} 人）"
    print(f"BLOCKED - {desc}")
    post_status("failure", desc)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # 任何异常都要落一个状态，避免必需检查永远挂起
        print(f"ERROR: {exc}", file=sys.stderr)
        try:
            post_status("error", f"检查自身出错：{exc}")
        except Exception:
            pass
        sys.exit(1)
