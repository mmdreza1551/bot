# Merge conflicts on this branch

The GitHub UI is warning about merge conflicts because both branches edited the same
files (`config.py`, `main.py`, and `messaging.py`) in overlapping sections. Git
cannot auto-merge those hunks, so it requires a manual resolution before the PR can
merge.

## How to resolve
1. Pull the latest changes from the target branch (usually `main`).
2. Run `git merge <target-branch>` locally to reproduce the conflicts.
3. Open each conflicted file and decide which version to keep or how to combine the
   changes. The conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`) show the two
   versions.
4. After fixing the files, run `git add` on them, then `git commit` to record the
   resolution.
5. Push the updated branch. The PR will update automatically and the conflict warning
   will disappear once the merge commit is present.

If the same settings or logic were removed or renamed in both branches (for example
background customization code that was deleted on one side while admin defaults were
edited on the other), those overlapping edits trigger the conflict message even
though the desired end state is straightforward.
