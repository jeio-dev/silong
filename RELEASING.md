Run a guided release for this project. Stop and wait for my answer at each step marked (confirm).

1. Should we release?
   - Read the "## Unreleased" section of CHANGELOG.md.
   - If it's empty, or has only internal entries (docs, tests, tooling, refactors), recommend not releasing and stop.
   - If the last release was very recent and nothing is urgent, say so and recommend waiting.

2. Classify and propose a version (confirm)
   - List each Unreleased entry with its class: breaking, feature, fix, or internal.
   - Propose the next version and name the rule that produced it:
     - 1.0 or later: breaking = MAJOR, feature = MINOR, fix = PATCH
     - Before 1.0: breaking = MINOR, everything else = PATCH
     - Internal-only changes never trigger a release on their own.
   - If I override the version, record the override and my reason.

3. Check readiness. Any failure stops the release.
   - All work in this batch is merged to main.
   - No blocking review comment is open.
   - The gate command from GITFLOW.md passes on main: python -m unittest discover -s tests -v
   - CI is green on the latest main commit, if CI exists (gh run list).

4. Release steps
   - If RELEASING.md exists, walk its steps one at a time and record evidence for each.
   - If it doesn't, use these generic steps:
     a. Bump the version in every file that holds it, and list those files.
     b. Rename "## Unreleased" to "## YYYY-MM-DD, Version X.Y.Z" and add a fresh empty "## Unreleased" above it.
     c. Write release notes: notable changes first (breaking changes at the top), then all commits since the last tag, grouped by scope.

5. Commit
   - Message: chore(release): vX.Y.Z
   - Stage only the files changed for the release.

6. Tag and publish (confirm)
   - Print the exact commands (git tag vX.Y.Z, git push origin vX.Y.Z, gh release create vX.Y.Z), then wait.
   - Never run them without my explicit go-ahead for this release.

7. If RELEASING.md didn't exist, offer to save the steps you performed in step 4 as a new RELEASING.md, so the next release repeats them.

Hotfix, only for a broken install, data loss, or a security issue: branch from the last tag, apply only the fix, release as a PATCH, then bring the fix back into main.

For web apps that deploy on merge to main: skip steps 2, 4a and 6. A merge is the release.