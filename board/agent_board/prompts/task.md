# Task $key: $title

Board command: `$board_cli`. Your card key is `$key`.

## What to do
$description

## Done when
$done_when

## Where
- Kind: $kind
- Repository: $repo
- Worktree (work only here): $worktree
- Base branch: $base
$deps_text

## Project rules
$agent_rules

## First steps
1. `$board_cli status $key in_progress`
2. Work inside `$worktree`. Do not commit and do not push.
3. Keep the card current with `$board_cli update $key ...`; finish with `$board_cli status $key in_review`.
