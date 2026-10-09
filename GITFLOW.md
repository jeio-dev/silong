Follow this git workflow for this project.

Branching
- One branch per phase, named phase/NN-slug (e.g. phase/03-login). Never commit directly to main.

Commits
- One commit per verified task, made only after the task's checks pass.
- Format: type(scope): summary (NN-TN)
  e.g. feat(auth): validate login form (03-T2)
- Types: feat, fix, refactor, test, docs, chore, ci. Use feat! or fix! for breaking changes.
- Scopes, use only these: auth, api, ui, db, deps, ci, docs
  If a change fits none of them, stop and ask me before adding a new scope.
- Stage only the files this task changed. Never use git add -A or git add .
  My own uncommitted edits must stay unstaged.
- Never commit while the gate command fails: python -m unittest discover -s tests -v

Push and PR (authorized)
- Push the phase branch after each commit.
- On the first push, open a draft PR. Mark it ready for review when all tasks in the phase are done.
- PR body, unless .github/pull_request_template.md exists:
```plaintext
  ## Summary — one paragraph on what the phase delivers
  ## Phase — plan file path, plus "Closes #N" if there is an issue
  ## Changes — one line per user-visible change with its task ID
  ## Verification — gate result with commit SHA, plus task-specific evidence
  ## Risk — none, or the high-risk tasks and how they were reviewed
  ## Release note — feature | fix | breaking | internal: one line
```

Always ask me first
- Merging into main, tagging, publishing a release, force-pushing, rewriting pushed history, deleting branches.
- Opening an issue.
