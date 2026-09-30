---
description: Have Codex review the current work, record findings in the ledger, fix them, and repeat
argument-hint: [optional focus for the reviewer]
---
Run an adversarial review loop with Codex. Follow these steps exactly.

1. If the Base line in .review/ledger.md is not a real commit sha, set it to the output of `git rev-parse HEAD~0` before any work on this task, and fill in the Task line.
2. Run the project's test command and note whether it passes.
3. Run Codex as the reviewer with a 10-minute timeout:
   codex exec --sandbox read-only -o .review/last-review.md "You are a strict code reviewer. Read .review/ledger.md for the task, open issues and history. Review all changes since the Base commit with `git diff <Base>` including uncommitted changes. Test result: <PASS or FAIL with the failing output>. For every issue ID in the ledger that is open or disputed, say FIXED, STILL OPEN, or WONTFIX with a one-line reason. Then list genuinely new problems as NEW with severity (high/medium/low), file, and a one-line claim; do not restate issues already in the ledger. Extra focus: $ARGUMENTS. End with exactly one line: VERDICT: APPROVE or VERDICT: CHANGES."
   Do not include your own explanations or reasoning in this prompt. Codex should judge the diff, the tests and the ledger only.
4. Read .review/last-review.md and update .review/ledger.md: give each NEW issue the next ISS-n ID, update the status of existing issues, and append a line under Rounds with the round number, verdict and test result.
5. Do not accept findings blindly. For each open issue, either fix it or mark it disputed in the ledger with a concrete reason Codex will see next round.
6. Run the tests again and commit with message "review round N".
7. Repeat from step 2 until the verdict is APPROVE, or 3 rounds have run, or the same issues stayed open for two rounds in a row. Then stop and give me a short summary of what was fixed, what is disputed, and anything that needs my decision.
