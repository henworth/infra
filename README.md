# infra — Preview Environments CDK App

The control plane for two FastAPI services ([`pantry`](https://github.com/your-org/pantry), [`shopping-list`](https://github.com/your-org/shopping-list)) and their ephemeral per-feature-branch preview environments on AWS. Python CDK, managed with `uv`.

Solves the "single shared dev environment" problem by spinning up isolated copies of the stack per feature branch, with cross-repo branch correlation so a "feature group" spanning both services gets a single combined env.

## Feature-group rule

The feature-group identifier is the **exact branch name**, across the two service repos.

- Branch on `pantry` only → `pantry-<branch>` env (pantry@branch + shopping-list@main).
- Branch on `shopping-list` only → `shopping-<branch>` env (pantry@main + shopping-list@branch).
- Same branch name on both → single `fg-<branch>` env (both services at branch).
- Different branch names on both → two independent solo envs (one per repo).
- Close one side of an `fg-...` env → demote to a solo env for the still-open side.
- Close both sides → destroy.

The full algorithm lives in [`scripts/reconcile.py`](scripts/reconcile.py).

## Stacks

- **`BaselineStack`** (long-lived, deployed once per region): VPC + NAT, Aurora Serverless v2 cluster, ECR repos, GitHub OIDC provider + deploy role, and the `db_bootstrap` Lambda invoked by per-env stacks.
- **`EnvironmentStack-<envName>`** (one per preview env): a CFN custom resource that creates per-env logical databases on the shared Aurora cluster, plus two container-image Lambdas (`pantry`, `shopping-list`) each with its own [Lambda Function URL](https://docs.aws.amazon.com/lambda/latest/dg/urls-configuration.html). Auth is `NONE` — the URLs are unguessable and the environments are short-lived preview-only.

```mermaid
flowchart LR
    GhaPantry["GHA: pantry repo"]
    GhaShopping["GHA: shopping-list repo"]
    subgraph baseline [BaselineStack: long-lived]
        VPC["VPC + NAT"]
        Aurora[("Aurora Serverless v2 Postgres")]
        EcrPantry[("ECR: pantry")]
        EcrShopping[("ECR: shopping-list")]
        OIDC[GitHub OIDC + DeployRole]
        Bootstrap["db_bootstrap Lambda"]
    end
    subgraph envFG ["EnvironmentStack-fg-feat-foo"]
        FnPantry["pantry Lambda + Function URL"]
        FnShopping["shopping-list Lambda + Function URL"]
        DBs[("pantry_db_fg_feat_foo + shopping_db_fg_feat_foo")]
    end
    GhaPantry -->|"push image"| EcrPantry
    GhaShopping -->|"push image"| EcrShopping
    GhaPantry -->|"OIDC + cdk deploy/destroy"| baseline
    GhaShopping -->|"OIDC + cdk deploy/destroy"| baseline
    EcrPantry --> FnPantry
    EcrShopping --> FnShopping
    FnShopping -->|"HTTPS to PANTRY_INTERNAL_URL"| FnPantry
    FnPantry --> Aurora
    FnShopping --> Aurora
    Bootstrap -.->|"creates"| DBs
    Aurora --- DBs
```

### Why Lambda (and not Fargate)

Preview environments are idle most of the time and get destroyed within days of being created. That pattern fits Lambda's pricing model exactly:

- **Cost.** A Fargate task at 0.25 vCPU / 0.5 GB is ~$8/mo always-on; five concurrent preview envs × 2 services = $80/mo of baseline whether anyone uses them. Lambdas at preview traffic rates are functionally free.
- **Spin-up.** A new Lambda + Function URL is ready in seconds. New Fargate services need to pull images, start containers, and pass ALB target health checks (60–120s).
- **Smaller infra per env.** No target group, no listener rule, no ECS service, no Service Connect namespace, no NAT-vs-public-subnet decisions. Per-env stack is two Lambdas + two Function URLs + a DB-bootstrap custom resource.

Trade-offs: cold starts (~1–2s on the first request to a recently-idle env) and inter-service calls happen over public HTTPS (Function URL → public internet → Function URL) rather than in-VPC Service Connect. Both are acceptable for previews. For a production-shape deployment you'd likely want API Gateway or an ALB in front (with WAF / auth) and possibly RDS Proxy.

## Local dev: full-stack demo (no AWS)

[`docker-compose.dev.yaml`](docker-compose.dev.yaml) runs both services + Postgres locally with hot reload. It expects the `pantry` and `shopping-list` repos checked out as siblings of this repo:

```plaintext
workspace/
  infra/           # this repo
  pantry/
  shopping-list/
```

Run from this directory:

```bash
docker compose -f docker-compose.dev.yaml up --build -d

curl http://localhost:8000/pantry/healthz
curl http://localhost:8001/shopping/healthz

# End-to-end purchase flow: shopping-list → pantry via the in-cluster client
curl -X POST http://localhost:8001/shopping/list \
  -H 'content-type: application/json' \
  -d '{"name":"Milk","category":"Dairy","quantity":2,"unit":"L"}'

curl -X POST http://localhost:8001/shopping/list/1/purchase
curl http://localhost:8000/pantry/items   # should now contain Milk
```

## CDK synth + deploy

```bash
uv sync
bash scripts/build-lambdas.sh             # vendor pg8000 into the Lambda bundle
npx -y aws-cdk synth                      # synth BaselineStack only
npx -y aws-cdk synth -c envName=demo \
  -c pantryImageTag=main -c shoppingImageTag=main   # also synth EnvironmentStack-demo
```

To deploy to real AWS (first time, from a workstation with admin creds):

```bash
npx -y aws-cdk bootstrap aws://<ACCOUNT_ID>/us-east-1
GITHUB_ORG=<your-org> npx -y aws-cdk deploy BaselineStack
```

After that, GitHub Actions assumes the OIDC role and handles all `EnvironmentStack-*` deploys.

### Context flags

| Flag | Purpose |
| --- | --- |
| `-c envName=...` | Also synth/deploy the per-env stack with this name |
| `-c pantryImageTag=...` | ECR image tag for pantry (default `main`) |
| `-c shoppingImageTag=...` | ECR image tag for shopping-list (default `main`) |
| `-c githubOrg=...` | GitHub org for the OIDC trust policy |
| `-c ministack=true` | Strip OIDC + custom resources + inter-SG ingress + RDS for Ministack |
| `-c dbHost=...` `-c dbPort=...` | Required with `ministack=true` for env stacks; supplied by `cdklocal-deploy.sh` |

## Reconcile script

[`scripts/reconcile.py`](scripts/reconcile.py) decides which CDK stacks to deploy/destroy for a given branch event. The GitHub Actions workflow in each service repo invokes it on `push` / `pull_request` events.

```bash
# Real-CI invocation: looks up the other repo's branch via the GitHub API
uv run python scripts/reconcile.py \
  --this-repo pantry --other-repo shopping-list \
  --branch feat/checkout \
  --event upsert \
  --this-image-tag $(git rev-parse HEAD)
```

For local exploration (no real repos required), use `--simulate-other-has-branch` and `--dry-run` to exercise all four state transitions:

```bash
# 1. Solo PR on pantry  →  deploy `pantry-feat-checkout` (pantry@abc123 + shopping@main)
uv run python scripts/reconcile.py \
  --this-repo pantry --other-repo shopping-list \
  --branch feat/checkout --event upsert --this-image-tag abc123 \
  --simulate-other-has-branch false --dry-run

# 2. Matching PR opens on shopping-list  →  PROMOTE: destroy solos, deploy `fg-feat-checkout`
uv run python scripts/reconcile.py \
  --this-repo shopping-list --other-repo pantry \
  --branch feat/checkout --event upsert --this-image-tag def456 \
  --simulate-other-has-branch true --simulate-other-sha abc123 --dry-run

# 3. Close pantry's PR while shopping-list's stays open  →  DEMOTE
uv run python scripts/reconcile.py \
  --this-repo pantry --other-repo shopping-list \
  --branch feat/checkout --event delete \
  --simulate-other-has-branch true --simulate-other-sha def456 --dry-run

# 4. Close shopping-list's PR  →  TEAR DOWN remaining envs
uv run python scripts/reconcile.py \
  --this-repo shopping-list --other-repo pantry \
  --branch feat/checkout --event delete \
  --simulate-other-has-branch false --dry-run
```

## Bootstrap scripts

[`scripts/bootstrap-dbs.py`](scripts/bootstrap-dbs.py) is the host-side replacement for the `db_bootstrap` Lambda in Ministack mode. It reads master Postgres credentials from Secrets Manager and creates/drops per-env logical databases:

```bash
uv run python scripts/bootstrap-dbs.py create --env-name fg-checkout
uv run python scripts/bootstrap-dbs.py drop   --env-name fg-checkout
```

[`scripts/ministack-create-db.py`](scripts/ministack-create-db.py) is the Ministack-only step that pre-creates the DB subnet group and a Postgres DB instance via the RDS API (Ministack's CFN engine doesn't provision `AWS::RDS::DBSubnetGroup`/`DBInstance`). It probes the resulting container for connection readiness before exiting and emits host- vs container-reachable endpoints.

## Ministack support (best-effort)

We chose [Ministack](https://ministack.org/) over LocalStack for local AWS emulation. The CDK code supports a `-c ministack=true` flag that strips/swaps constructs Ministack can't run:

- OIDC provider → `AccountRootPrincipal`-trust role
- `cr.Provider` DB bootstrap → host-side [`scripts/bootstrap-dbs.py`](scripts/bootstrap-dbs.py)
- Aurora cluster → host-side [`scripts/ministack-create-db.py`](scripts/ministack-create-db.py) (creates a Postgres DB instance via the RDS API)
- NAT gateways → omitted (Lambdas run without VPC config in Ministack mode; the Ministack-managed Postgres container is reachable via `host.docker.internal`)
- Service Lambdas: deployed without VPC config (no SG-to-SG ingress, no ENI provisioning quirks)

Bring up the emulator (state persists in `./.ministack/`):

```bash
docker compose -f docker-compose.ministack.yaml up -d
bash scripts/ministack-up.sh                # push initial images
bash scripts/cdklocal-deploy.sh fg-demo     # deploys baseline, creates RDS+DBs, deploys env
```

## GitHub configuration

Three repos, three secret/var scopes. `AWS_DEPLOY_ROLE` is the role ARN exported by `BaselineStack` as `GhaDeployRoleArn`.

### `infra` repo

| Name | Type | Purpose |
| --- | --- | --- |
| `AWS_DEPLOY_ROLE` | variable | Same role ARN used by both service repos |

### `pantry` and `shopping-list` repos

| Name | Type | Purpose |
| --- | --- | --- |
| `AWS_DEPLOY_ROLE` | variable | ARN of `GhaDeployRole` from `BaselineStack` outputs |
| `INFRA_REPO_TOKEN` | secret | Token with `contents: read` on the `infra` repo (PAT or GitHub App) |
| `CROSS_REPO_READ_TOKEN` | secret | Token with `contents: read` on the *other* service repo |
| `INFRA_REPO` | variable | e.g. `your-org/infra` |

If you have a GitHub org, promote `AWS_DEPLOY_ROLE` and `INFRA_REPO` to org-level. The two read tokens are ideal candidates for a single GitHub App installed on all three repos.

### Pre-flight checklist before pushing your first feature branch

1. `npx aws-cdk bootstrap aws://<account>/<region>` once per account+region.
2. From a workstation with admin creds, deploy `BaselineStack` once manually: `GITHUB_ORG=<your-org> npx aws-cdk deploy BaselineStack`. This creates the OIDC provider and `GhaDeployRole`.
3. Copy the `GhaDeployRoleArn` output and set it as `AWS_DEPLOY_ROLE` in all three repos (or at the org level).
4. Set the remaining secrets/vars as above.
5. Push to `main` in each service repo to seed the `:main` image tags in ECR (the reconciler uses these as the "stable" tag for the non-feature side of a solo env).
6. Push a feature branch and open a PR. `preview.yml` runs the reconciler.

## What's intentionally not implemented

- Auth on the Function URLs (`AUTH_NONE`; URLs are unguessable but unauthenticated). For production this becomes `AWS_IAM` + SigV4 inter-service calls, or fronting with API Gateway + Cognito.
- Custom domain / HTTPS cert (Function URLs come with their own `*.lambda-url.<region>.on.aws` HTTPS endpoint).
- Alembic migrations (using `Base.metadata.create_all` on startup).
- Async SQLAlchemy.
- RDS Proxy (recommended at production scale to avoid connection storms from many warm Lambdas).
- Seeding per-env databases from `main` (`pg_dump | pg_restore` hook in the bootstrap Lambda is an easy add).
- A DynamoDB env-registry (CloudFormation stack listing is the source of truth).
- Tests beyond `/healthz` smoke checks.
