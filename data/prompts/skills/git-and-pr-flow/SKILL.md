---
name: git-and-pr-flow
description: Commit work, branch, push and open or review a pull request with the git and github verbs - named paths only, push and PR only when the owner asked
license: MIT
metadata:
  polyrob-when: 'Committing work, branching or opening a pull request'
  polyrob-priority: '3'
  polyrob-owner-only: 'true'
  polyrob-triggers: '{"tool_ids":["git","github"]}'
  polyrob-version: '1'
---
# Git and Pull-Request Flow

Record work in git and move it through a pull request. A push, a PR, a comment and an
issue are public; do them only when the owner asked.

## Tool availability
Read the `git` and `github` lines of your `<tool-catalog>`. `[loadable]` →
`load_tool("<id>")`. `[gated:…]` → follow the remedy it names (see `harness-manual`);
absent → say that this deploy has no such tool. `github` also needs a token the operator sets. A delegated sub-agent
holds neither. The git verbs work in the session workspace (on a local terminal, the
project directory).

## Commit
1. `git_status` and `git_diff` first: know what changed and what is not yours.
2. Branch for a change when the owner works with branches:
   `git_checkout(ref="<branch>", create=true)`. `git_branch` lists branches.
3. Stage NAMED paths only: `git_add(paths=["src/a.py", "tests/test_a.py"])`. Never
   stage everything; never stage a secret, a `.env` or a build output.
4. `git_commit(message=…)` — say WHY in the message, not only what.
5. `git_log` to confirm the commit.

## Push and pull request (only when the owner asked)
1. `git_pull` before `git_push`; resolve a conflict in your own change only.
2. `git_push(branch=…, set_upstream=true)` for a new branch.
3. `github_open_pr(repo="<owner>/<name>", title=…, head="<branch>", base="main", body=…)`.
   The body says what changed, why, and how you tested it.
4. CI: `github_actions_runs(repo=…)`, then `github_actions_logs(repo=…, run_id=…)` for a
   failed run (see `coding-workflow` → `build-failure-triage.md`). `github_actions_logs`
   gives the run status and a logs URL, not the log text.
5. Never merge without the owner's word: `github_merge_pr` is public and final.

`git_push`, `github_open_pr` and `github_merge_pr` ask the owner only at a high
compute posture or when the operator gates them; otherwise they run at once.
`github_pr_comment` and `github_issue_create` never ask. So the owner's request is
the gate: do not rely on an approval prompt.

## Review a pull request
`github_pr_view(repo=…, number=…)` gives the title, state and URL only. For the diff,
`git_clone` and `shell_run` `git diff <base>...<head>` where `shell` is loaded; else say
that you could not read the diff. Read the diff with the lenses in
`coding-workflow` → `review-lenses.md`. Post findings with `github_pr_comment` only when
the owner asked you to comment; else report them to the owner.

## Failures
- Auth refused → the token is an operator setting; say so, do not ask for it in chat.
- A push rejected as non-fast-forward → `git_pull`, re-run the tests, push again.
