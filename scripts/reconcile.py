#!/usr/bin/env python3
"""Reconcile preview environments from a GitHub event.

Called by the per-repo `preview.yml` GitHub Actions workflow.

Inputs (CLI flags, all required unless noted):
  --this-repo      pantry | shopping-list
  --other-repo     pantry | shopping-list
  --branch         feature branch name (head_ref). Main is rejected.
  --event          upsert | delete
  --this-image-tag commit SHA pushed to ECR (required for upsert)

Optional env:
  GITHUB_TOKEN     used for `gh api repos/...` calls
  GITHUB_ORG       org/owner; default "your-org"
  AWS_REGION       defaults to "us-east-1"
  CDK_DIR          path to the CDK app directory (defaults to ../ relative to this file)
  CDK_BIN          override for the CDK CLI; default "npx -y aws-cdk"

The script is intentionally side-effecty: it shells out to `cdk deploy/destroy`
with the right context flags. It is idempotent because CDK is idempotent.

Algorithm (matches the plan):
  upsert:
    if other repo has matching branch -> deploy fg-<branch> (destroy any
    prior solo envs for this branch in both repos first).
    else                                -> deploy <this-repo>-<branch>
    (destroying fg-<branch> if it exists from a prior promotion).
  delete:
    if other repo has matching branch -> destroy fg-<branch>, deploy
    <other-repo>-<branch> for the remaining side.
    else                                -> destroy both possible solo envs
    and fg-<branch> defensively.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import httpx

REPO_SHORT = {"pantry": "pantry", "shopping-list": "shopping"}


@dataclasses.dataclass(frozen=True)
class Repo:
    name: str  # "pantry" | "shopping-list"

    @property
    def short(self) -> str:
        return REPO_SHORT[self.name]


def slug(branch: str) -> str:
    s = branch.lower().replace("/", "-")
    s = re.sub(r"[^a-z0-9-]+", "-", s).strip("-")
    return s[:40] or "branch"


def solo_env(repo: Repo, branch: str) -> str:
    return f"{repo.short}-{slug(branch)}"


def group_env(branch: str) -> str:
    return f"fg-{slug(branch)}"


def github_has_branch(org: str, repo: str, branch: str, token: str | None) -> bool:
    url = f"https://api.github.com/repos/{org}/{repo}/branches/{branch}"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = httpx.get(url, headers=headers, timeout=10)
    if resp.status_code == 200:
        return True
    if resp.status_code == 404:
        return False
    raise RuntimeError(f"GitHub API error checking {org}/{repo}@{branch}: {resp.status_code} {resp.text}")


def github_branch_sha(org: str, repo: str, branch: str, token: str | None) -> str:
    url = f"https://api.github.com/repos/{org}/{repo}/branches/{branch}"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = httpx.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()["commit"]["sha"]


def stack_exists(stack_name: str, cdk_dir: Path, cdk_bin: list[str]) -> bool:
    """Use CDK to check; we treat exit code 0 with the stack in output as exists."""
    cmd = cdk_bin + ["ls"]
    proc = subprocess.run(cmd, cwd=cdk_dir, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        return False
    return stack_name in {line.strip() for line in proc.stdout.splitlines()}


def cdk_deploy(
    env_name: str,
    *,
    pantry_tag: str,
    shopping_tag: str,
    cdk_dir: Path,
    cdk_bin: list[str],
) -> None:
    stack_name = f"EnvironmentStack-{env_name}"
    cmd = cdk_bin + [
        "deploy",
        "--require-approval",
        "never",
        "BaselineStack",
        stack_name,
        "-c",
        f"envName={env_name}",
        "-c",
        f"pantryImageTag={pantry_tag}",
        "-c",
        f"shoppingImageTag={shopping_tag}",
    ]
    print(f"+ {shlex.join(cmd)}", flush=True)
    subprocess.run(cmd, cwd=cdk_dir, check=True)


def cdk_destroy(
    env_name: str,
    *,
    cdk_dir: Path,
    cdk_bin: list[str],
) -> None:
    stack_name = f"EnvironmentStack-{env_name}"
    cmd = cdk_bin + [
        "destroy",
        "--force",
        stack_name,
        "-c",
        f"envName={env_name}",
        "-c",
        "pantryImageTag=main",
        "-c",
        "shoppingImageTag=main",
    ]
    print(f"+ {shlex.join(cmd)}", flush=True)
    subprocess.run(cmd, cwd=cdk_dir, check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--this-repo", required=True, choices=list(REPO_SHORT))
    parser.add_argument("--other-repo", required=True, choices=list(REPO_SHORT))
    parser.add_argument("--branch", required=True)
    parser.add_argument("--event", required=True, choices=("upsert", "delete"))
    parser.add_argument("--this-image-tag", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--simulate-other-has-branch",
        choices=("auto", "true", "false"),
        default="auto",
        help="Override the GitHub API branch-existence check. Useful when the "
        "real repos don't exist yet (demos, local testing).",
    )
    parser.add_argument(
        "--simulate-other-sha",
        default="simulated-other-sha",
        help="Pretend SHA for the other repo's branch tip when --simulate-other-has-branch=true. Ignored otherwise.",
    )
    args = parser.parse_args()

    if args.branch.lower() in {"main", "master"}:
        print("branch is main/master; nothing to reconcile.", file=sys.stderr)
        return 0
    if args.this_repo == args.other_repo:
        print("this-repo and other-repo must differ.", file=sys.stderr)
        return 2

    org = os.environ.get("GITHUB_ORG", "your-org")
    token = os.environ.get("GITHUB_TOKEN")
    cdk_dir = Path(os.environ.get("CDK_DIR", str(Path(__file__).resolve().parent.parent)))
    cdk_bin = shlex.split(os.environ.get("CDK_BIN", "npx -y aws-cdk"))

    this_repo = Repo(args.this_repo)
    other_repo = Repo(args.other_repo)
    branch = args.branch

    if args.simulate_other_has_branch == "true":
        other_has = True
    elif args.simulate_other_has_branch == "false":
        other_has = False
    else:
        other_has = github_has_branch(org, other_repo.name, branch, token)
    print(
        json.dumps(
            {
                "event": args.event,
                "this_repo": this_repo.name,
                "other_repo": other_repo.name,
                "branch": branch,
                "other_has_branch": other_has,
            }
        ),
        flush=True,
    )

    solo_this = solo_env(this_repo, branch)
    solo_other = solo_env(other_repo, branch)
    fg = group_env(branch)

    def deploy(name: str, pantry_tag: str, shopping_tag: str) -> None:
        if args.dry_run:
            print(f"[dry-run] deploy {name} pantry_tag={pantry_tag} shopping_tag={shopping_tag}")
            return
        cdk_deploy(
            name,
            pantry_tag=pantry_tag,
            shopping_tag=shopping_tag,
            cdk_dir=cdk_dir,
            cdk_bin=cdk_bin,
        )

    def destroy(name: str) -> None:
        if args.dry_run:
            print(f"[dry-run] destroy {name}")
            return
        cdk_destroy(name, cdk_dir=cdk_dir, cdk_bin=cdk_bin)

    if args.event == "upsert":
        if not args.this_image_tag:
            print("--this-image-tag is required for upsert.", file=sys.stderr)
            return 2

        if other_has:
            if args.simulate_other_has_branch == "true":
                other_tag = args.simulate_other_sha
            else:
                other_tag = github_branch_sha(org, other_repo.name, branch, token)
            this_tag = args.this_image_tag
            pantry_tag, shopping_tag = (this_tag, other_tag) if this_repo.name == "pantry" else (other_tag, this_tag)
            destroy(solo_this)
            destroy(solo_other)
            deploy(fg, pantry_tag=pantry_tag, shopping_tag=shopping_tag)
        else:
            destroy(fg)
            this_tag = args.this_image_tag
            pantry_tag, shopping_tag = (this_tag, "main") if this_repo.name == "pantry" else ("main", this_tag)
            deploy(solo_this, pantry_tag=pantry_tag, shopping_tag=shopping_tag)
        return 0

    # delete
    if other_has:
        if args.simulate_other_has_branch == "true":
            other_tag = args.simulate_other_sha
        else:
            other_tag = github_branch_sha(org, other_repo.name, branch, token)
        destroy(fg)
        destroy(solo_this)
        pantry_tag, shopping_tag = ("main", other_tag) if this_repo.name == "pantry" else (other_tag, "main")
        deploy(solo_other, pantry_tag=pantry_tag, shopping_tag=shopping_tag)
    else:
        destroy(fg)
        destroy(solo_this)
        destroy(solo_other)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
