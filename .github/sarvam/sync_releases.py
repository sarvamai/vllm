# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Mirror published stable releases and propose them to Sarvam main."""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

WORKFLOW = ".github/workflows/sarvam-sync-upstream-releases.yml"
PROJECTS = {
    "sarvamai/vllm": ("vllm-project/vllm", "vllm"),
    "sarvamai/sglang": ("sgl-project/sglang", "sglang"),
}


def run(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def api(endpoint, method="GET"):
    return json.loads(run("gh", "api", "--method", method, endpoint) or "null")


def pages(endpoint, key=None):
    result = []
    for page in range(1, 1001):
        batch = api(f"{endpoint}?per_page=100&page={page}")
        batch = batch[key] if key else batch
        result.extend(batch)
        if len(batch) < 100:
            return result
    raise RuntimeError("API pagination limit reached")


def stable(release):
    return (
        not release["draft"]
        and not release["prerelease"]
        and version(release) is not None
        and bool(release["published_at"])
    )


def version(release):
    tag = release["tag_name"]
    if not tag.startswith("v"):
        return None
    base, separator, post = tag[1:].partition(".post")
    parts = base.split(".")
    if len(parts) != 3:
        return None
    parts.append(post if separator else "0")
    if not all(part.isascii() and part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def select_releases(releases, mirrors, prefix):
    """Bootstrap with the newest version, then include later publications."""
    releases = [release for release in releases if stable(release)]
    if not releases:
        raise RuntimeError("No published stable project releases found")
    release_prefix = f"{prefix}/release-"
    mirrored_tags = {
        name.removeprefix(release_prefix)
        for name in mirrors
        if name.startswith(release_prefix)
    }
    if mirrored_tags - {release["tag_name"] for release in releases}:
        raise RuntimeError("Existing release mirrors have no upstream release metadata")
    known = [
        release
        for release in releases
        if f"{prefix}/release-{release['tag_name']}" in mirrors
    ]
    if not known:
        return [max(releases, key=version)]
    baseline = min(release["published_at"] for release in known)
    return sorted(
        (release for release in releases if release["published_at"] >= baseline),
        key=lambda release: (release["published_at"], version(release)),
    )


def check_workflows(paths, workflows):
    states = {workflow["path"]: workflow["state"] for workflow in workflows}
    unsafe = [path for path in paths if states.get(path) != "disabled_manually"]
    if unsafe:
        raise RuntimeError(
            "Upstream workflows must be registered and disabled before mirroring: "
            + ", ".join(sorted(unsafe))
            + ". See .github/sarvam/README.md. No refs were pushed."
        )


def disable_upstream(repo, dry_run):
    workflows = pages(f"repos/{repo}/actions/workflows", "workflows")
    if not workflows:
        raise RuntimeError(
            "No registered workflows; complete the documented activation first"
        )
    for workflow in workflows:
        path = workflow["path"]
        if not path.startswith(".github/workflows/") or path == WORKFLOW:
            continue
        if workflow["state"] == "disabled_manually":
            continue
        print(f"Disable upstream workflow: {path}")
        if not dry_run:
            api(f"repos/{repo}/actions/workflows/{workflow['id']}/disable", "PUT")


def sync(repo, dry_run):
    upstream, prefix = PROJECTS[repo]
    branches = pages(f"repos/{repo}/branches")
    mirrors = {branch["name"]: branch["commit"]["sha"] for branch in branches}
    if "main" not in mirrors:
        raise RuntimeError("Sarvam main must already exist")
    releases = select_releases(pages(f"repos/{upstream}/releases"), mirrors, prefix)
    workflows = pages(f"repos/{repo}/actions/workflows", "workflows")
    if not any(w["path"] == WORKFLOW and w["state"] == "active" for w in workflows):
        raise RuntimeError("Sarvam sync workflow must be registered and active")
    open_prs = pages(f"repos/{repo}/pulls")
    with tempfile.TemporaryDirectory(prefix="sarvam-release-sync-") as directory:

        def git(*args):
            return run("git", *args, cwd=directory)

        def ancestor(base, head):
            result = subprocess.run(
                ["git", "merge-base", "--is-ancestor", base, head],
                cwd=directory,
                check=False,
            )
            if result.returncode not in (0, 1):
                raise RuntimeError("Could not check commit ancestry")
            return result.returncode == 0

        git("init", "--bare", "--quiet")
        git("remote", "add", "origin", f"https://github.com/{repo}.git")
        git("remote", "add", "upstream", f"https://github.com/{upstream}.git")
        git(
            "fetch",
            "--quiet",
            "--no-tags",
            "origin",
            "+refs/heads/main:refs/heads/target-main",
            f"+refs/heads/{prefix}/*:refs/remotes/origin/{prefix}/*",
        )
        git(
            "fetch",
            "--quiet",
            "--no-tags",
            "upstream",
            *[f"refs/tags/{r['tag_name']}:refs/tags/{r['tag_name']}" for r in releases],
        )
        plans = {}
        shas = {}
        for release in releases:
            tag = release["tag_name"]
            sha = git("rev-parse", f"refs/tags/{tag}^{{commit}}")
            shas[tag] = sha
            branch = f"{prefix}/release-{tag}"
            if branch in mirrors and mirrors[branch] != sha:
                raise RuntimeError(
                    f"Immutable release changed: {branch}; refusing to rewrite"
                )
            if branch not in mirrors:
                plans[branch] = sha
        latest = max(releases, key=version)
        latest_sha = shas[latest["tag_name"]]
        rolling = f"{prefix}/main"
        if mirrors.get(rolling) != latest_sha:
            if rolling in mirrors and not ancestor(mirrors[rolling], latest_sha):
                raise RuntimeError(f"Refusing non-fast-forward update of {rolling}")
            plans[rolling] = latest_sha
        paths = set()
        for sha in set(shas.values()):
            paths.update(
                path
                for path in git(
                    "ls-tree", "-r", "--name-only", sha, ".github/workflows"
                ).splitlines()
                if PurePosixPath(path).parent == PurePosixPath(".github/workflows")
                and PurePosixPath(path).suffix in {".yml", ".yaml"}
            )
        if WORKFLOW in paths:
            raise RuntimeError(
                "Upstream release collides with our reserved sync workflow path"
            )
        check_workflows(paths, workflows)
        for branch, sha in plans.items():
            print(f"Mirror {branch} -> {sha}")
        if plans and not dry_run:
            git(
                "push",
                "--atomic",
                "origin",
                *[f"{sha}:refs/heads/{branch}" for branch, sha in plans.items()],
            )
            actual = dict(
                (ref.removeprefix("refs/heads/"), sha)
                for sha, ref in (
                    line.split()
                    for line in git("ls-remote", "--heads", "origin").splitlines()
                )
            )
            if any(actual.get(branch) != sha for branch, sha in plans.items()):
                raise RuntimeError("Remote SHA verification failed; no PRs created")
        for release in releases:
            tag = release["tag_name"]
            sha = shas[tag]
            branch = f"{prefix}/release-{tag}"
            if ancestor(sha, "refs/heads/target-main"):
                print(f"Already integrated: {tag}")
                continue
            if any(
                pr["base"]["ref"] == "main"
                and pr["head"]["ref"] == branch
                and (pr["head"].get("repo") or {}).get("full_name") == repo
                for pr in open_prs
            ):
                print(f"PR already open: {branch}")
                continue
            print(f"Draft PR: {branch} -> main")
            if dry_run:
                continue
            body = Path(directory) / "pr-body.md"
            body.write_text(
                f"Import upstream stable release `{tag}` into Sarvam main.\n\n"
                f"- Release: https://github.com/{upstream}/releases/tag/{tag}\n"
                f"- Published: {release['published_at']}\n"
                f"- Exact upstream commit: `{sha}`\n\n"
                "The release branch is immutable. Preserve Sarvam sync files and keep "
                "upstream workflows disabled. Use a merge commit to retain ancestry. "
                "No model validation has run automatically; record correctness and "
                "serving-performance results before merging.\n"
            )
            run(
                "gh",
                "pr",
                "create",
                "--repo",
                repo,
                "--base",
                "main",
                "--head",
                branch,
                "--draft",
                "--title",
                f"chore(sync): upstream release {tag}",
                "--body-file",
                str(body),
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, choices=sorted(PROJECTS))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--disable-upstream-workflows", action="store_true")
    args = parser.parse_args()
    if not os.environ.get("GH_TOKEN"):
        parser.error("GH_TOKEN must be provided through the environment")
    if args.disable_upstream_workflows:
        disable_upstream(args.repo, args.dry_run)
    else:
        sync(args.repo, args.dry_run)


if __name__ == "__main__":
    main()
