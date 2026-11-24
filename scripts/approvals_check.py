#!/usr/bin/env python3
"""最小可用版「策略驱动的批准检查」。

流程：
  1. 读仓库里的 .approvals.yml（策略）
  2. 拉这个 PR 改了哪些文件
  3. 按路径匹配规则，得出「需要几个人批准」
  4. 拉 PR 的评审记录，数出「实际有几个有效批准」
  5. 把结论作为 commit status 报回 GitHub（名字 approvals-policy）
  6. 不满足则退出码 1

环境变量：
  GITHUB_TOKEN  由 Actions 注入
  REPO          owner/repo
  PR_NUMBER     PR 编号
  HEAD_SHA      PR 头提交，用于挂 status
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

def gh(path: str) -> list:
    """GET 一个分页接口，返回全部条目。"""
    items: list = []
    page = 1
    while True:
        sep = "&" if "?" in path else "?"
        url = f"{API}{path}{sep}per_page=100&page={page}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {os.environ['GITHUB_TOKEN']}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "approvals-check")
        with urllib.request.urlopen(req, timeout=30) as resp:
            chunk = json.loads(resp.read().decode("utf-8"))
        if not isinstance(chunk, list) or not chunk:
            break
        items.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
    return items


def gh_one(path: str) -> dict:
    """GET 一个返回单个对象的接口。"""
    req = urllib.request.Request(f"{API}{path}")
    req.add_header("Authorization", f"Bearer {os.environ['GITHUB_TOKEN']}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "approvals-check")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_status(state: str, description: str) -> None:
    url = f"{API}/repos/{os.environ['REPO']}/statuses/{os.environ['HEAD_SHA']}"
    body = json.dumps({
        "state": state,
        "context": STATUS_NAME,
        "description": description[:140],
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {os.environ['GITHUB_TOKEN']}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "approvals-check")
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


# ---------- 策略 ----------

def load_policy() -> dict:
    """只支持我们用到的那一点 YAML 结构，避免额外依赖。

    这是刻意的取舍：真要上生产就换成 pyyaml，这里为了少一个依赖。
    """
    if not os.path.exists(POLICY_PATH):
        return {"rules": []}

    rules: list[dict] = []
    cur: dict | None = None
    in_paths = False

    with open(POLICY_PATH, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].rstrip()
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip())
            text = line.strip()

            if text.startswith("- name:"):
                cur = {"name": text.split(":", 1)[1].strip(), "paths": [], "seats": 1}
                rules.append(cur)
                in_paths = False
            elif cur is not None and text.startswith("paths:"):
                in_paths = True
            elif cur is not None and in_paths and text.startswith("- "):
                cur["paths"].append(text[2:].strip().strip('"').strip("'"))
            elif cur is not None and text.startswith("seats:"):
                cur["seats"] = int(text.split(":", 1)[1].strip())
                in_paths = False
    return {"rules": rules}


def glob_to_regex(pattern: str) -> re.Pattern:
    """把 ** / * / ? 转成正则，** 可跨层级。"""
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


def required_seats(policy: dict, changed: list[str]) -> tuple[int, str]:
    best, rule_name = 0, "-"
    for rule in policy["rules"]:
        pats = [glob_to_regex(p) for p in rule["paths"]]
        if any(p.match(f) for p in pats for f in changed):
            if rule["seats"] > best:
                best, rule_name = rule["seats"], rule["name"]
    return best, rule_name


# ---------- 评审 ----------

def count_approvals(reviews: list, author: str) -> tuple[int, list[str]]:
    """每个评审人只认最新一次状态；排除作者本人和机器人。"""
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

    pr_info = gh_one(f"/repos/{repo}/pulls/{pr}")
    author = (pr_info.get("user") or {}).get("login", "")

    changed = [f["filename"] for f in gh(f"/repos/{repo}/pulls/{pr}/files")]
    policy = load_policy()
    need, rule_name = required_seats(policy, changed)
    have, who = count_approvals(gh(f"/repos/{repo}/pulls/{pr}/reviews"), author)

    print(f"PR #{pr} by {author}")
    print(f"changed files ({len(changed)}): {', '.join(changed)}")
    print(f"matched rule: {rule_name}")
    print(f"seats required: {need}")
    print(f"approvals: {have} {who}")

    if need == 0:
        desc = "没有匹配到审批规则，放行"
        print(f"PASS - {desc}")
        post_status("success", desc)
        return 0

    if have >= need:
        desc = f"批准 {have}/{need}，满足策略（{rule_name}）"
        print(f"PASS - {desc}")
        post_status("success", desc)
        return 0

    desc = f"还差 {need - have} 人批准（{rule_name} 要求 {need} 人）"
    print(f"BLOCKED - {desc}")
    post_status("failure", desc)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # 失败也要落一个 status，避免必需检查永远挂起
        print(f"ERROR: {exc}", file=sys.stderr)
        try:
            post_status("error", f"检查自身出错：{exc}")
        except Exception:
            pass
        sys.exit(1)
