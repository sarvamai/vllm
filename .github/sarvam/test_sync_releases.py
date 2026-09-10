# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Offline tests for release selection, workflow safety, and real Git syncs."""

import contextlib
import importlib.util
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "sync_releases", Path(__file__).with_name("sync_releases.py")
)
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


def release(tag, date="2026-09-01T00:00:00Z", **kwargs):
    return dict(
        tag_name=tag, published_at=date, draft=False, prerelease=False, **kwargs
    )


class ReleaseTests(unittest.TestCase):
    def test_bootstrap_skips_prereleases_drafts_and_kernel_releases(self):
        candidates = [
            release("v0.5.18"),
            release("v0.5.19"),
            release("sgl-kernel-v1.0.0"),
        ]
        candidates += [dict(release("v0.6.0"), prerelease=True)]
        candidates += [dict(release("v1.0.0"), draft=True)]
        self.assertEqual(
            sync.select_releases(candidates, {}, "sglang"), [candidates[1]]
        )

    def test_version_parser_rejects_nonstable_and_malformed_tags(self):
        for tag in (
            "v0.5.19rc1",
            "v0.5.19-rc.1",
            "v0.5",
            "v0.5.19.post",
            "v0.5.19.post1.post2",
            "v0.5.-1",
            "sgl-kernel-v0.5.19",
        ):
            with self.subTest(tag=tag):
                self.assertFalse(sync.stable(release(tag)))

    def test_post_releases_and_numeric_version_order(self):
        candidates = [release("v0.9.9"), release("v0.10.0"), release("v0.10.0.post1")]
        self.assertEqual(sync.select_releases(candidates, {}, "vllm"), [candidates[2]])

    def test_multiple_releases_and_late_maintenance_are_not_lost(self):
        candidates = [release("v0.5.19"), release("v0.5.20", "2026-09-02T00:00:00Z")]
        candidates += [release("v0.5.18.post1", "2026-09-03T00:00:00Z")]
        mirrors = {"sglang/release-v0.5.19": "abc"}
        self.assertEqual(
            sync.select_releases(candidates, mirrors, "sglang"), candidates
        )

    def test_missing_baseline_metadata_stops_instead_of_rebootstrapping(self):
        with self.assertRaisesRegex(RuntimeError, "no upstream release metadata"):
            sync.select_releases(
                [release("v0.5.20")], {"sglang/release-v0.5.19": "abc"}, "sglang"
            )

    def test_unregistered_or_active_workflows_block_sync(self):
        path = ".github/workflows/upstream.yml"
        for state in (None, "active", "disabled_inactivity", "disabled_fork"):
            workflows = [] if state is None else [dict(path=path, state=state)]
            with self.assertRaisesRegex(RuntimeError, "registered and disabled"):
                sync.check_workflows([path], workflows)
        sync.check_workflows([path], [dict(path=path, state="disabled_manually")])

    def test_disable_keeps_our_workflow_enabled(self):
        workflows = [dict(path=sync.WORKFLOW, state="active", id=1)]
        workflows += [dict(path=".github/workflows/upstream.yml", state="active", id=2)]
        with (
            patch.object(sync, "pages", return_value=workflows),
            patch.object(sync, "api") as api,
        ):
            sync.disable_upstream("sarvamai/vllm", True)
            api.assert_not_called()
            sync.disable_upstream("sarvamai/vllm", False)
            api.assert_called_once_with(
                "repos/sarvamai/vllm/actions/workflows/2/disable", "PUT"
            )


class GitSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.upstream = self.root / "upstream"
        self.origin = self.root / "origin.git"
        self.upstream.mkdir()
        self.git("init", "-q", "-b", "main", cwd=self.upstream)
        self.git("config", "user.name", "Test", cwd=self.upstream)
        self.git("config", "user.email", "test@example.com", cwd=self.upstream)
        (self.upstream / "code.txt").write_text("base\n")
        self.git("add", ".", cwd=self.upstream)
        self.git("commit", "-qm", "base", cwd=self.upstream)
        self.base = self.git("rev-parse", "HEAD", cwd=self.upstream)
        self.git("clone", "--bare", "-q", str(self.upstream), str(self.origin))
        (self.upstream / "code.txt").write_text("release\n")
        self.git("commit", "-qam", "release", cwd=self.upstream)
        self.sha = self.git("rev-parse", "HEAD", cwd=self.upstream)
        self.git("tag", "-a", "v0.5.19", "-m", "release", cwd=self.upstream)
        self.releases = [release("v0.5.19")]
        self.prs = []
        self.workflows = [dict(path=sync.WORKFLOW, state="active")]
        original_run = sync.run

        def fake_run(*args, cwd=None):
            if args[:3] == ("gh", "pr", "create"):
                body = Path(args[args.index("--body-file") + 1]).read_text()
                self.assertIn(self.sha, body)
                self.prs.append(
                    dict(
                        base=dict(ref="main"),
                        head=dict(
                            ref=args[args.index("--head") + 1],
                            repo=dict(full_name="sarvamai/sglang"),
                        ),
                    )
                )
                return "https://github.com/sarvamai/sglang/pull/123"
            if args[:3] == ("git", "remote", "add"):
                args = (
                    *args[:-1],
                    str(self.origin if args[3] == "origin" else self.upstream),
                )
            return original_run(*args, cwd=cwd)

        def fake_pages(endpoint, key=None):
            if endpoint.endswith("/branches"):
                rows = self.git(
                    "for-each-ref",
                    "--format=%(refname:short) %(objectname)",
                    "refs/heads",
                    cwd=self.origin,
                )
                return [
                    dict(name=name, commit=dict(sha=sha))
                    for name, sha in (line.split() for line in rows.splitlines())
                ]
            if endpoint.endswith("/releases"):
                return self.releases
            if endpoint.endswith("/workflows"):
                return self.workflows
            if endpoint.endswith("/pulls"):
                return self.prs
            self.fail(endpoint)

        self.addCleanup(patch.stopall)
        patch.object(sync, "run", side_effect=fake_run).start()
        patch.object(sync, "pages", side_effect=fake_pages).start()

    def git(self, *args, cwd=None):
        return subprocess.check_output(
            ["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, text=True
        ).strip()

    def test_dry_run_then_atomic_mirrors_and_idempotent_pr(self):
        sync.sync("sarvamai/sglang", True)
        self.assertEqual(
            self.git("branch", "--format=%(refname:short)", cwd=self.origin), "main"
        )
        self.assertFalse(self.prs)
        sync.sync("sarvamai/sglang", False)
        for branch in ("sglang/main", "sglang/release-v0.5.19"):
            self.assertEqual(self.git("rev-parse", branch, cwd=self.origin), self.sha)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), self.base)
        self.assertEqual(len(self.prs), 1)
        sync.sync("sarvamai/sglang", False)
        self.assertEqual(len(self.prs), 1)

    def test_moved_release_tag_cannot_rewrite_immutable_branch(self):
        sync.sync("sarvamai/sglang", False)
        self.git("tag", "-f", "v0.5.19", self.base, cwd=self.upstream)
        with self.assertRaisesRegex(RuntimeError, "Immutable release changed"):
            sync.sync("sarvamai/sglang", False)

    def test_divergent_rolling_mirror_aborts_all_updates(self):
        self.git("checkout", "-qb", "divergent", self.base, cwd=self.upstream)
        (self.upstream / "other.txt").write_text("divergent\n")
        self.git("add", ".", cwd=self.upstream)
        self.git("commit", "-qm", "divergent", cwd=self.upstream)
        self.git(
            "push",
            "-q",
            str(self.origin),
            "HEAD:refs/heads/sglang/main",
            cwd=self.upstream,
        )
        with self.assertRaisesRegex(RuntimeError, "non-fast-forward"):
            sync.sync("sarvamai/sglang", False)
        self.assertNotIn("sglang/release-", self.git("branch", cwd=self.origin))
        self.assertFalse(self.prs)

    def test_unknown_upstream_workflow_blocks_before_any_push(self):
        path = self.upstream / ".github/workflows/new.yml"
        path.parent.mkdir(parents=True)
        path.write_text("name: new\non: push\njobs: {}\n")
        self.git("add", ".", cwd=self.upstream)
        self.git("commit", "-qm", "new workflow", cwd=self.upstream)
        self.git("tag", "-f", "v0.5.19", cwd=self.upstream)
        with self.assertRaisesRegex(RuntimeError, "registered and disabled"):
            sync.sync("sarvamai/sglang", False)
        self.assertEqual(
            self.git("branch", "--format=%(refname:short)", cwd=self.origin), "main"
        )
        self.assertFalse(self.prs)

    def test_release_already_in_main_does_not_open_pr(self):
        self.git("push", "-q", str(self.origin), "HEAD:main", cwd=self.upstream)
        sync.sync("sarvamai/sglang", False)
        self.assertFalse(self.prs)


if __name__ == "__main__":
    with contextlib.redirect_stdout(io.StringIO()):
        unittest.main()
