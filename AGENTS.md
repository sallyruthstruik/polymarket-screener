# Task implementation


## Feature task

For each user feature task, first create implementation plan. When creating plan, inspect code history, related PRs.
Interview me relentlessly about every aspect of this plan until we reach a shared understanding. Walk down each branch of the design tree, resolving dependencies between decisions one-by-one. For each question, provide your recommended answer.

Ask the questions one at a time.

If a question can be answered by exploring the codebase, explore the codebase instead.

For each task create branch and PR. Work until PR actions are green.

DO NOT CREATE WORKTREES BY YOURSELF. Only checkout branch, or ask user if he wants to create worktree.

## Small task

Small tasks (bugfixes, etc.) must be done without new branch

# Code

Keep code simple. Try to keep each feature in separated folder in service layer. Try to write orthogonal code.
Don't use protocols without reason, prefer normal classes.
Don't use dataclasses. Use pydantic models for structured data.

Use mypy heavily. Type all input/output parameters.
Use linters heavily.
For front code use typescript only.
Don't use any in both TS and python without reason for that.

Commit all steps


## Logging policy

All conditional branches must be logged
All cycles must be logged (each nth step)
All http requests must be logged (request, response[:100])