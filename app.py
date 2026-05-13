#!/usr/bin/env python3
"""CDK app entry point.

Always synths `BaselineStack` (the long-lived control plane). When the context
key `envName` is provided, ALSO synths `EnvironmentStack-<envName>` for an
ephemeral preview environment. Image tags for the two services are passed via
`-c pantryImageTag=...` and `-c shoppingImageTag=...`.

Stacks are environment-agnostic: CFN intrinsics (e.g. `Fn::GetAZs`) resolve at
deploy time against whichever account/region the CDK CLI is pointed at (real
AWS or Ministack). Set CDK_DEFAULT_ACCOUNT / CDK_DEFAULT_REGION if you want to
pin them, or rely on the AWS CLI's resolution chain.
"""

import os

import aws_cdk as cdk

from stacks.baseline_stack import BaselineStack
from stacks.environment_stack import EnvironmentStack

app = cdk.App()

github_org = app.node.try_get_context("githubOrg") or os.environ.get("GITHUB_ORG", "your-org")
# When `-c ministack=true`, skip CDK constructs Ministack can't run:
#   - The GitHub OIDC provider (Ministack doesn't support AWS::IAM::OIDCProvider
#     natively, and the L2 construct's custom-resource Lambda can't reach IAM).
#   - The cr.Provider-based db_bootstrap custom resource (the framework Lambda
#     can't reach the Aurora endpoint from inside a Lambda container).
# In Ministack mode, those features are emulated by host-side scripts.
ministack_mode = str(app.node.try_get_context("ministack") or "").lower() in {
    "1",
    "true",
    "yes",
}

baseline = BaselineStack(
    app,
    "BaselineStack",
    github_org=github_org,
    ministack_mode=ministack_mode,
)

env_name = app.node.try_get_context("envName")
if env_name:
    EnvironmentStack(
        app,
        f"EnvironmentStack-{env_name}",
        env_name=env_name,
        pantry_image_tag=app.node.try_get_context("pantryImageTag") or "main",
        shopping_image_tag=app.node.try_get_context("shoppingImageTag") or "main",
        baseline=baseline,
        ministack_mode=ministack_mode,
        db_host=app.node.try_get_context("dbHost"),
        db_port=app.node.try_get_context("dbPort"),
    )

app.synth()
