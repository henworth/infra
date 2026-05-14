"""Per-env ephemeral preview environment.

Builds, for one branch (or feature group):
  - a CFN custom resource that creates the per-env logical databases on the
    shared Aurora cluster (via the db_bootstrap Lambda in BaselineStack);
  - two container-image Lambdas (pantry, shopping-list) using the per-env
    image tags pushed to the shared ECR repos;
  - a Function URL per Lambda (auth=NONE) so each service is reachable over
    HTTPS at `https://<random>.lambda-url.<region>.on.aws/`.

shopping-list's `PANTRY_INTERNAL_URL` env var is wired to pantry's Function
URL so the in-cluster purchase flow works.

When `ministack_mode=True`:
  - the cr.Provider-based DB bootstrap is skipped (the framework Lambdas can't
    reach the Aurora endpoint from inside Ministack's Lambda containers; the
    deploy script invokes scripts/bootstrap-dbs.py from the host instead);
  - service Lambdas are launched without VPC config (Ministack-managed Postgres
    is reached via `host.docker.internal` supplied through `db_host`).
"""

from aws_cdk import (
    CfnOutput,
    CustomResource,
    Duration,
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_lambda as _lambda,
    aws_logs as logs,
    custom_resources as cr,
)
from constructs import Construct

from stacks.baseline_stack import BaselineStack


class EnvironmentStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env_name: str,
        pantry_image_tag: str,
        shopping_image_tag: str,
        baseline: BaselineStack,
        ministack_mode: bool = False,
        db_host: str | None = None,
        db_port: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.env_name = env_name

        env_slug = env_name.replace("-", "_")
        pantry_db = f"pantry_db_{env_slug}"
        shopping_db = f"shopping_db_{env_slug}"
        pantry_api_prefix = "/pantry"
        shopping_api_prefix = "/shopping"

        if ministack_mode:
            db_setup = None
        else:
            assert baseline.db_bootstrap_fn is not None, (
                "BaselineStack.db_bootstrap_fn is required when ministack_mode=False"
            )
            provider_log_group = logs.LogGroup(
                self,
                "DbBootstrapProviderLogs",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=RemovalPolicy.DESTROY,
            )
            provider = cr.Provider(
                self,
                "DbBootstrapProvider",
                on_event_handler=baseline.db_bootstrap_fn,  # pyright: ignore[reportArgumentType]
                provider_function_name=None,
                log_group=provider_log_group,
            )
            db_setup = CustomResource(
                self,
                "DbBootstrap",
                service_token=provider.service_token,
                properties={
                    "EnvName": env_name,
                    "DatabaseNames": [pantry_db, shopping_db],
                },
            )

        if ministack_mode:
            if not (db_host and db_port):
                raise RuntimeError(
                    "ministack_mode=True requires `db_host` and `db_port` to be "
                    "passed explicitly (set them via -c dbHost / -c dbPort from "
                    "the deploy script after scripts/ministack-create-db.py runs)."
                )
            resolved_db_host = db_host
            resolved_db_port = db_port
        else:
            assert baseline.db_cluster is not None
            resolved_db_host = baseline.db_cluster.cluster_endpoint.hostname
            resolved_db_port = str(baseline.db_cluster.cluster_endpoint.port)

        # The DB password lives in Secrets Manager. Lambda env vars don't
        # support live secret resolution like ECS does, so we bake the username
        # + password into a DATABASE_URL via CFN dynamic references (resolved
        # at deploy time). `DatabaseSecret`'s default exclusion list keeps the
        # password URL-safe, so no encoding is required.
        db_username = baseline.db_secret.secret_value_from_json("username").unsafe_unwrap()
        db_password = baseline.db_secret.secret_value_from_json("password").unsafe_unwrap()

        def _database_url(db_name: str) -> str:
            return f"postgresql+psycopg://{db_username}:{db_password}@{resolved_db_host}:{resolved_db_port}/{db_name}"

        common_lambda_kwargs: dict = dict(
            timeout=Duration.seconds(30),
            memory_size=512,
            architecture=_lambda.Architecture.X86_64,
        )
        if not ministack_mode:
            common_lambda_kwargs.update(
                vpc=baseline.vpc,
                vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
                security_groups=[baseline.service_sg],
            )

        def _service_lambda(
            logical_id: str,
            image_repo: ecr.IRepository,
            image_tag: str,
            api_prefix: str,
            db_name: str,
            extra_env: dict[str, str],
        ) -> _lambda.DockerImageFunction:
            log_group = logs.LogGroup(
                self,
                f"{logical_id}Logs",
                log_group_name=f"/aws/lambda/preview-{env_name}-{logical_id.lower()}",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=RemovalPolicy.DESTROY,
            )
            fn = _lambda.DockerImageFunction(
                self,
                f"{logical_id}Fn",
                function_name=f"preview-{env_name}-{logical_id.lower()}",
                code=_lambda.DockerImageCode.from_ecr(
                    image_repo,  # pyright: ignore[reportArgumentType]
                    tag_or_digest=image_tag,
                ),
                environment={
                    "API_PREFIX": api_prefix,
                    "DATABASE_URL": _database_url(db_name),
                    **extra_env,
                },
                log_group=log_group,
                **common_lambda_kwargs,
            )
            if db_setup is not None:
                fn.node.add_dependency(db_setup)
            return fn

        self.pantry_fn = _service_lambda(
            "Pantry",
            baseline.ecr_pantry,
            pantry_image_tag,
            pantry_api_prefix,
            pantry_db,
            extra_env={},
        )
        pantry_fn_url = self.pantry_fn.add_function_url(
            auth_type=_lambda.FunctionUrlAuthType.NONE,
            cors=_lambda.FunctionUrlCorsOptions(
                allowed_origins=["*"],
                allowed_methods=[_lambda.HttpMethod.ALL],
                allowed_headers=["*"],
            ),
        )

        # `pantry_fn_url.url` ends with `/`, so concat the prefix without leading slash.
        self.shopping_fn = _service_lambda(
            "Shopping",
            baseline.ecr_shopping,
            shopping_image_tag,
            shopping_api_prefix,
            shopping_db,
            extra_env={
                "PANTRY_INTERNAL_URL": f"{pantry_fn_url.url}pantry",
            },
        )
        shopping_fn_url = self.shopping_fn.add_function_url(
            auth_type=_lambda.FunctionUrlAuthType.NONE,
            cors=_lambda.FunctionUrlCorsOptions(
                allowed_origins=["*"],
                allowed_methods=[_lambda.HttpMethod.ALL],
                allowed_headers=["*"],
            ),
        )

        CfnOutput(self, "PreviewPantryUrl", value=f"{pantry_fn_url.url}pantry")
        CfnOutput(self, "PreviewShoppingUrl", value=f"{shopping_fn_url.url}shopping")
        CfnOutput(self, "EnvName", value=env_name)
