Interview me relentlessly about every aspect of this plan until we reach a shared understanding. Walk down each branch of the design tree, resolving dependencies between decisions one-by-one. For each question, provide your recommended answer.

Ask the questions one at a time.

If a question can be answered by exploring the codebase, explore the codebase instead.

For each task create branch and PR. Work until PR actions are not green.

Keep code simple. Try to keep each feature in separated folder in service layer. Try to write orthogonal code.
Don't use protocols without reason, prefer normal classes.

Use mypy heavily. Type all input/output parameters.
Use linters heavily.
For front code use typescript only.
Don't use any in both TS and python without reason for that.
