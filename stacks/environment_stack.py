"""Per-env ephemeral preview environment.

Builds: a per-env Cloud Map namespace for Service Connect, a custom resource
that creates the per-env logical databases on the shared Aurora cluster, two
Fargate services (pantry, shopping-list) with Service Connect, and listener
rules on the shared ALB at `/<envName>/pantry/*` and `/<envName>/shopping/*`.

When `ministack_mode=True`, the cr.Provider-based DB bootstrap custom resource
is omitted (its framework Lambda can't reach the Aurora endpoint on Ministack);
the deploy/reconcile scripts call scripts/bootstrap-dbs.py from the host before
the per-env services need their databases.
"""

import hashlib

from aws_cdk import (
    CfnOutput,
    CustomResource,
    Duration,
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_ecs as ecs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_iam as iam,
    aws_logs as logs,
    aws_servicediscovery as servicediscovery,
    custom_resources as cr,
)
from constructs import Construct

from stacks.baseline_stack import BaselineStack


def _priority_for(env_name: str) -> int:
    """Deterministic ALB listener-rule priority for an env (1000-40999)."""
    digest = hashlib.md5(env_name.encode("utf-8")).hexdigest()
    return (int(digest[:8], 16) % 40_000) + 1000


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
        pantry_prefix = f"/{env_name}/pantry"
        shopping_prefix = f"/{env_name}/shopping"

        sc_namespace = servicediscovery.PrivateDnsNamespace(
            self,
            "ServiceConnectNamespace",
            name=f"{env_name}.preview.local",
            vpc=baseline.vpc,
            description=f"Service Connect namespace for preview env {env_name}",
        )

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

        task_execution_role = iam.Role(
            self,
            "TaskExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),  # pyright: ignore[reportArgumentType]
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AmazonECSTaskExecutionRolePolicy")
            ],
        )
        task_role = iam.Role(
            self,
            "TaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),  # pyright: ignore[reportArgumentType]
        )
        baseline.db_secret.grant_read(task_role)

        log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name=f"/pantry/preview/{env_name}",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        secret_username = ecs.Secret.from_secrets_manager(baseline.db_secret, "username")  # pyright: ignore[reportArgumentType]
        secret_password = ecs.Secret.from_secrets_manager(baseline.db_secret, "password")  # pyright: ignore[reportArgumentType]
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

        def _service(
            logical_id: str,
            container_name: str,
            image_repo: ecr.IRepository,
            image_tag: str,
            api_prefix: str,
            db_name: str,
            extra_env: dict[str, str],
        ) -> tuple[ecs.FargateService, str]:
            task_def = ecs.FargateTaskDefinition(
                self,
                f"{logical_id}TaskDef",
                cpu=256,
                memory_limit_mib=512,
                execution_role=task_execution_role,  # pyright: ignore[reportArgumentType]
                task_role=task_role,  # pyright: ignore[reportArgumentType]
            )
            container = task_def.add_container(
                container_name,
                image=ecs.ContainerImage.from_ecr_repository(image_repo, image_tag),
                logging=ecs.LogDrivers.aws_logs(stream_prefix=container_name, log_group=log_group),
                environment={
                    "API_PREFIX": api_prefix,
                    "DB_HOST": resolved_db_host,
                    "DB_PORT": resolved_db_port,
                    "DB_NAME": db_name,
                    **extra_env,
                },
                secrets={
                    "DB_USERNAME": secret_username,
                    "DB_PASSWORD": secret_password,
                },
                command=[
                    "sh",
                    "-c",
                    'export DATABASE_URL="postgresql+psycopg://${DB_USERNAME}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}" && '
                    "exec uvicorn app.main:app --host 0.0.0.0 --port 8000",
                ],
            )
            container.add_port_mappings(
                ecs.PortMapping(
                    container_port=8000,
                    name=container_name,
                    app_protocol=ecs.AppProtocol.http,
                )
            )

            service = ecs.FargateService(
                self,
                f"{logical_id}Service",
                cluster=baseline.cluster,
                task_definition=task_def,
                desired_count=1,
                min_healthy_percent=0,
                max_healthy_percent=200,
                circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
                security_groups=[baseline.service_sg],
                # No NAT in ministack mode -> run tasks in public subnets so
                # they can still pull images from ECR.
                assign_public_ip=ministack_mode,
                vpc_subnets=ec2.SubnetSelection(
                    subnet_type=(ec2.SubnetType.PUBLIC if ministack_mode else ec2.SubnetType.PRIVATE_WITH_EGRESS)
                ),
                service_connect_configuration=ecs.ServiceConnectProps(
                    namespace=sc_namespace.namespace_arn,
                    services=[
                        ecs.ServiceConnectService(
                            port_mapping_name=container_name,
                            dns_name=container_name,
                            port=8000,
                        )
                    ],
                ),
            )

            if ministack_mode:
                # L1 path: build the target group as `CfnTargetGroup` and wire
                # `service.LoadBalancers` via a CFN property override. This
                # skips `attach_to_application_target_group`, which would
                # otherwise call `loadBalancer.connections.allowTo(service)`
                # and emit a standalone `AWS::EC2::SecurityGroupIngress`
                # (rejected by Ministack). Inbound 8000 is already open on the
                # shared SG via CIDR-peer ingress in BaselineStack.
                tg_cfn = elbv2.CfnTargetGroup(
                    self,
                    f"{logical_id}Tg",
                    port=8000,
                    protocol="HTTP",
                    target_type="ip",
                    vpc_id=baseline.vpc.vpc_id,
                    health_check_enabled=True,
                    health_check_path=f"{api_prefix}/healthz",
                    health_check_protocol="HTTP",
                    health_check_interval_seconds=15,
                    healthy_threshold_count=2,
                    matcher=elbv2.CfnTargetGroup.MatcherProperty(http_code="200"),
                    target_group_attributes=[
                        elbv2.CfnTargetGroup.TargetGroupAttributeProperty(
                            key="deregistration_delay.timeout_seconds",
                            value="10",
                        )
                    ],
                )
                cfn_service = service.node.default_child
                assert cfn_service is not None
                cfn_service.add_property_override(  # pyright: ignore[reportAttributeAccessIssue]
                    "LoadBalancers",
                    [
                        {
                            "ContainerName": container_name,
                            "ContainerPort": 8000,
                            "TargetGroupArn": tg_cfn.ref,
                        }
                    ],
                )
                service.node.add_dependency(tg_cfn)
                tg_arn = tg_cfn.ref
            else:
                tg = elbv2.ApplicationTargetGroup(
                    self,
                    f"{logical_id}Tg",
                    vpc=baseline.vpc,
                    port=8000,
                    protocol=elbv2.ApplicationProtocol.HTTP,
                    target_type=elbv2.TargetType.IP,
                    health_check=elbv2.HealthCheck(
                        path=f"{api_prefix}/healthz",
                        healthy_http_codes="200",
                        interval=Duration.seconds(15),
                        healthy_threshold_count=2,
                    ),
                    deregistration_delay=Duration.seconds(10),
                )
                service.attach_to_application_target_group(tg)
                tg_arn = tg.target_group_arn

            if db_setup is not None:
                service.node.add_dependency(db_setup)
            return service, tg_arn

        self.pantry_service, pantry_tg_arn = _service(
            "Pantry",
            "pantry",
            baseline.ecr_pantry,
            pantry_image_tag,
            pantry_prefix,
            pantry_db,
            extra_env={},
        )
        self.shopping_service, shopping_tg_arn = _service(
            "Shopping",
            "shopping",
            baseline.ecr_shopping,
            shopping_image_tag,
            shopping_prefix,
            shopping_db,
            extra_env={
                "PANTRY_INTERNAL_URL": (f"http://pantry.{env_name}.preview.local:8000{pantry_prefix}"),
            },
        )

        priority_base = _priority_for(env_name)

        def _rule(logical_id: str, prefix: str, tg_arn: str, priority: int) -> None:
            # Use L1 `CfnListenerRule` uniformly. The L2 `ApplicationListenerRule`
            # requires an `IApplicationTargetGroup`, but in ministack mode the
            # target group is built as L1 `CfnTargetGroup` so we only have the
            # ARN string. L1 rules work identically on real AWS.
            elbv2.CfnListenerRule(
                self,
                logical_id,
                listener_arn=baseline.listener.listener_arn,
                priority=priority,
                conditions=[
                    elbv2.CfnListenerRule.RuleConditionProperty(
                        field="path-pattern",
                        path_pattern_config=elbv2.CfnListenerRule.PathPatternConfigProperty(
                            values=[prefix, f"{prefix}/*"]
                        ),
                    )
                ],
                actions=[
                    elbv2.CfnListenerRule.ActionProperty(
                        type="forward",
                        target_group_arn=tg_arn,
                    )
                ],
            )

        _rule("PantryRule", pantry_prefix, pantry_tg_arn, priority_base)
        _rule("ShoppingRule", shopping_prefix, shopping_tg_arn, priority_base + 1)

        alb_dns = baseline.alb.load_balancer_dns_name
        CfnOutput(self, "PreviewPantryUrl", value=f"http://{alb_dns}{pantry_prefix}")
        CfnOutput(
            self,
            "PreviewShoppingUrl",
            value=f"http://{alb_dns}{shopping_prefix}",
        )
        CfnOutput(self, "EnvName", value=env_name)
