# Sarvam release sync

Every six hours, the opt-in workflow checks stable releases of `vllm-project/vllm`.
It mirrors exact upstream commits and opens draft PRs into Sarvam `main`.

- `vllm/release-vX.Y.Z` records an immutable release and is its PR's source.
- Tags `vX.Y.Z` and `vX.Y.Z.postN` qualify; drafts, prereleases and component
  releases are excluded. Bootstrap starts at the newest stable version. Later
  runs include every release published since that baseline, including maintenance
  releases and multiple releases between polls.
- Already-integrated releases need no PR; existing open PRs are reused. Closing an
  unmerged PR causes it to be proposed again. Release branches must be retained.
- Moved tags or missing release metadata stop synchronization.
  Ref updates are atomic and verified. No force pushes or automatic merges occur.
  Each release has its own branch because successive upstream tags can diverge.

## Setup

Repository settings and credentials are configured separately from this PR.

1. **Disable inherited workflows individually before merging or activating this
   setup.** Keep repository-level Actions enabled and enable only
   `sarvam-sync-upstream-releases.yml`. Upstream workflow files are unmodified;
   they have no additional Sarvam execution guards. An empty workflow list does
   not establish that workflows are disabled; resolve registration before activation.
2. Fix mirror ruleset `22532736` to match `refs/heads/vllm/*`; retain
   deletion and force-push protection. In main ruleset `22534074`, retain PR
   and conversation-resolution requirements, remove required linear history, and
   permit merge commits. Enable **Allow merge commits** in repository settings.
   Preserve existing bypass settings and do not require disabled upstream checks.
3. Store a fine-grained PAT as **`REPOSITORY_SYNC_TOKEN`**, scoped to the target
   Sarvam repo(s), with **Contents**, **Workflows**, **Actions**, and **Pull requests**
   write permissions. Its owner needs corresponding access, and organization
   approval may be required. Use GitHub Settings or the interactive
   `gh secret set REPOSITORY_SYNC_TOKEN` prompt. A GitHub App is not required.
4. From a trusted checkout, with that credential supplied as `GH_TOKEN`, run:

   ```bash
   python3 .github/sarvam/sync_releases.py --repo sarvamai/vllm --disable-upstream-workflows
   python3 .github/sarvam/sync_releases.py --repo sarvamai/vllm --dry-run
   ```

   Use a virtual environment's Python locally. Only the standard library, Git and
   `gh` are needed. Dry runs read GitHub and fetch Git into temporary directories;
   they do not change workflow states, remote refs or PRs.
5. After merging, manually run **Sarvam sync upstream releases** on `main`, first
   with `dry_run: true`, then with `dry_run: false`. Verify the mirrors and PRs.
   Set **`SARVAM_RELEASE_SYNC_ENABLED=true`** to enable the schedule. Until then,
   scheduled runs skip. Manual runs on other branches also skip.

## Workflow safety and release review

Every live run disables registered inherited workflows while retaining the Sarvam
sync workflow. Before any mirror push, every upstream workflow filename must be
registered and **`disabled_manually`**; inactivity/fork-disabled states do not count.
The script never checks out or executes upstream code.

A new workflow filename stops the sync before any push. Register it using an inert
workflow on a temporary branch (`push` restricted to that branch and a job with
`if: false`), verify and disable its registered ID, then retry. Do not register the original
executable workflow as a workaround. Remove the temporary branch afterwards.
Never re-enable inherited workflows: exact mirrors contain their original YAML.

Review conflicts, preserve Sarvam sync files, and keep upstream workflows disabled.
Record model correctness and serving-performance validation in each release PR.
Merge with a merge commit to retain upstream ancestry.
