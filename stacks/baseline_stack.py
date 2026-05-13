"""Long-lived baseline stack: VPC, Aurora SLv2, ECS cluster, ALB, ECR, GitHub OIDC, db_bootstrap Lambda.

When `ministack_mode=True`, OIDC and the db_bootstrap Lambda are omitted (those
features rely on Lambda containers that can't reach IAM / RDS on Ministack).
The deploy/reconcile scripts emulate the missing parts on the host instead.
"""

import os

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_ecs as ecs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_rds as rds,
)
from constructs import Construct


GITHUB_OIDC_THUMBPRINT = "6938fd4d98bab03faadb97b34396831e3780aea1"


class BaselineStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        github_org: str = "your-org",
        repo_pantry: str = "pantry",
        repo_shopping: str = "shopping-list",
        repo_infra: str = "infra",
        ministack_mode: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.ministack_mode = ministack_mode

        if ministack_mode:
            # Ministack rejects AWS::EC2::EIP, so no NAT gateways. Skip private
            # subnets with egress entirely; ECS tasks run in public subnets and
            # the DB (real Postgres container created out-of-band) lives in the
            # isolated subnets.
            self.vpc = ec2.Vpc(
                self,
                "Vpc",
                max_azs=2,
                nat_gateways=0,
                subnet_configuration=[
                    ec2.SubnetConfiguration(
                        name="public",
                        subnet_type=ec2.SubnetType.PUBLIC,
                        cidr_mask=24,
                    ),
                    ec2.SubnetConfiguration(
                        name="isolated",
                        subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                        cidr_mask=24,
                    ),
                ],
            )
        else:
            self.vpc = ec2.Vpc(
                self,
                "Vpc",
                max_azs=2,
                nat_gateways=1,
                subnet_configuration=[
                    ec2.SubnetConfiguration(
                        name="public",
                        subnet_type=ec2.SubnetType.PUBLIC,
                        cidr_mask=24,
                    ),
                    ec2.SubnetConfiguration(
                        name="private",
                        subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                        cidr_mask=24,
                    ),
                    ec2.SubnetConfiguration(
                        name="isolated",
                        subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                        cidr_mask=24,
                    ),
                ],
            )

        if ministack_mode:
            # Ministack rejects standalone `AWS::EC2::SecurityGroupIngress`
            # resources, which CDK emits any time it adds an SG-to-SG ingress
            # rule (this happens automatically when an ALB attaches to a
            # target group, and even for from-self rules). Workaround:
            # collapse to a single shared SG with CIDR-only inline ingress.
            # CIDR-peer rules always inline as the SG's SecurityGroupIngress
            # property; the L2 ALB construct's auto-added SG-to-SG rules then
            # become no-ops because everyone is already in the same SG and
            # all ports are already open.
            shared_sg = ec2.SecurityGroup(
                self,
                "SharedSg",
                vpc=self.vpc,
                description="Ministack-only shared SG for ALB, services, and DB",
                allow_all_outbound=True,
            )
            shared_sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80), "HTTP")
            shared_sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(8000), "service port")
            shared_sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(5432), "postgres")
            self.alb_sg = shared_sg
            self.service_sg = shared_sg
            self.db_sg = shared_sg
        else:
            self.alb_sg = ec2.SecurityGroup(self, "AlbSg", vpc=self.vpc, description="Pantry preview ALB")
            self.alb_sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80), "HTTP from anywhere")
            self.service_sg = ec2.SecurityGroup(self, "ServiceSg", vpc=self.vpc, description="Pantry ECS tasks")
            self.service_sg.add_ingress_rule(self.alb_sg, ec2.Port.tcp(8000), "ALB to tasks")
            self.service_sg.add_ingress_rule(self.service_sg, ec2.Port.tcp(8000), "intra-env Service Connect")
            self.db_sg = ec2.SecurityGroup(self, "DbSg", vpc=self.vpc, description="Aurora cluster")
            self.db_sg.add_ingress_rule(self.service_sg, ec2.Port.tcp(5432), "tasks and lambda to Postgres")

        self.db_secret = rds.DatabaseSecret(self, "DbSecret", username="postgres")

        if ministack_mode:
            # Ministack's CFN engine only provisions `AWS::RDS::DBCluster` and
            # rejects `AWS::RDS::DBSubnetGroup` / `AWS::RDS::DBInstance`. The
            # L2 `rds.DatabaseCluster` construct emits all three, so we skip
            # it entirely and let scripts/ministack-create-db.py create the
            # DB instance via the RDS API after baseline deploys. The
            # resulting endpoint is passed into EnvironmentStack via context.
            self.db_cluster = None  # type: ignore[assignment]
            self.db_subnet_ids = [
                s.subnet_id for s in self.vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED).subnets
            ]
        else:
            self.db_cluster = rds.DatabaseCluster(
                self,
                "PantryAurora",
                engine=rds.DatabaseClusterEngine.aurora_postgres(version=rds.AuroraPostgresEngineVersion.VER_16_4),
                credentials=rds.Credentials.from_secret(self.db_secret),  # type: ignore[arg-type]
                vpc=self.vpc,
                vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
                security_groups=[self.db_sg],
                serverless_v2_min_capacity=0.5,
                serverless_v2_max_capacity=4,
                writer=rds.ClusterInstance.serverless_v2("Writer"),
                default_database_name="postgres",
                removal_policy=RemovalPolicy.DESTROY,
                deletion_protection=False,
            )

        self.cluster = ecs.Cluster(
            self,
            "EcsCluster",
            vpc=self.vpc,
        )

        self.alb = elbv2.ApplicationLoadBalancer(
            self,
            "Alb",
            vpc=self.vpc,
            internet_facing=True,
            security_group=self.alb_sg,
        )
        self.listener = self.alb.add_listener(
            "HttpListener",
            port=80,
            default_action=elbv2.ListenerAction.fixed_response(
                404,
                content_type="text/plain",
                message_body="no preview env matched this path",
            ),
        )

        # `empty_on_delete=True` and lifecycle rules add a custom-resource Lambda
        # (`AutoDeleteImages`) that Ministack can't run. Skip those features in
        # Ministack mode; on real AWS they keep ECR tidy across re-deploys.
        ecr_extras = (
            dict()
            if ministack_mode
            else dict(
                empty_on_delete=True,
                lifecycle_rules=[
                    ecr.LifecycleRule(
                        max_image_count=50,
                        rule_priority=1,
                        tag_status=ecr.TagStatus.UNTAGGED,
                    )
                ],
            )
        )
        self.ecr_pantry = ecr.Repository(
            self,
            "EcrPantry",
            repository_name="pantry",
            image_scan_on_push=True,
            removal_policy=RemovalPolicy.DESTROY,
            **ecr_extras,  # pyright: ignore[reportArgumentType]
        )
        self.ecr_shopping = ecr.Repository(
            self,
            "EcrShopping",
            repository_name="shopping-list",
            image_scan_on_push=True,
            removal_policy=RemovalPolicy.DESTROY,
            **ecr_extras,  # pyright: ignore[reportArgumentType]
        )

        if ministack_mode:
            # Ministack doesn't support AWS::IAM::OIDCProvider natively, and the
            # CDK custom-resource Lambda hangs because the in-container AWS SDK
            # can't reach the IAM endpoint. The role is still created so the
            # downstream stacks have a deploy principal, but with no GitHub
            # federation in this mode.
            self.deploy_role = iam.Role(
                self,
                "GhaDeployRole",
                assumed_by=iam.AccountRootPrincipal(),  # pyright: ignore[reportArgumentType]
                description="Local/Ministack stand-in for the real OIDC-federated deploy role.",
                max_session_duration=Duration.hours(1),
            )
        else:
            oidc_cfn = iam.CfnOIDCProvider(
                self,
                "GitHubOidc",
                url="https://token.actions.githubusercontent.com",
                client_id_list=["sts.amazonaws.com"],
                thumbprint_list=[GITHUB_OIDC_THUMBPRINT],
            )
            repo_subs = [
                f"repo:{github_org}/{repo_pantry}:*",
                f"repo:{github_org}/{repo_shopping}:*",
                f"repo:{github_org}/{repo_infra}:*",
            ]
            self.deploy_role = iam.Role(
                self,
                "GhaDeployRole",
                assumed_by=iam.WebIdentityPrincipal(  # pyright: ignore[reportArgumentType]
                    oidc_cfn.ref,
                    conditions={
                        "StringLike": {"token.actions.githubusercontent.com:sub": repo_subs},
                        "StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"},
                    },
                ),
                description="Assumed by GitHub Actions to deploy preview environments.",
                max_session_duration=Duration.hours(1),
            )

        self.deploy_role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("PowerUserAccess"))
        self.deploy_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "iam:CreateRole",
                    "iam:DeleteRole",
                    "iam:GetRole",
                    "iam:PassRole",
                    "iam:AttachRolePolicy",
                    "iam:DetachRolePolicy",
                    "iam:PutRolePolicy",
                    "iam:DeleteRolePolicy",
                    "iam:TagRole",
                    "iam:UntagRole",
                ],
                resources=["*"],
            )
        )

        # The db_bootstrap Lambda is only useful when EnvironmentStack invokes it
        # via a CFN custom resource. On Ministack we skip it because the
        # cr.Provider framework Lambdas also can't reach the cluster endpoint
        # from inside a Lambda container; a host-side script (scripts/bootstrap-dbs.py)
        # creates the per-env databases instead.
        if ministack_mode:
            self.db_bootstrap_fn = None
        else:
            bootstrap_asset = "lambdas/db_bootstrap/build"
            if not os.path.isdir(bootstrap_asset):
                raise RuntimeError(
                    f"Lambda bundle not found at {bootstrap_asset}. Run scripts/build-lambdas.sh before `cdk synth`."
                )
            self.db_bootstrap_log_group = logs.LogGroup(
                self,
                "DbBootstrapLogs",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=RemovalPolicy.DESTROY,
            )
            self.db_bootstrap_fn = _lambda.Function(
                self,
                "DbBootstrap",
                runtime=_lambda.Runtime.PYTHON_3_12,
                handler="handler.on_event",
                code=_lambda.Code.from_asset(bootstrap_asset),
                timeout=Duration.seconds(60),
                vpc=self.vpc,
                vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
                security_groups=[self.service_sg],
                log_group=self.db_bootstrap_log_group,
                environment={
                    "DB_SECRET_ARN": self.db_secret.secret_arn,
                    "DB_HOST": self.db_cluster.cluster_endpoint.hostname,  # pyright: ignore[reportOptionalMemberAccess]
                    "DB_PORT": str(self.db_cluster.cluster_endpoint.port),  # pyright: ignore[reportOptionalMemberAccess]
                },
            )
            self.db_secret.grant_read(self.db_bootstrap_fn)

        CfnOutput(self, "AlbDnsName", value=self.alb.load_balancer_dns_name)
        CfnOutput(self, "ClusterName", value=self.cluster.cluster_name)
        CfnOutput(self, "EcrPantryUri", value=self.ecr_pantry.repository_uri)
        CfnOutput(self, "EcrShoppingUri", value=self.ecr_shopping.repository_uri)
        CfnOutput(self, "GhaDeployRoleArn", value=self.deploy_role.role_arn)
        CfnOutput(self, "DbSecretArn", value=self.db_secret.secret_arn)
        CfnOutput(self, "VpcId", value=self.vpc.vpc_id)
        CfnOutput(self, "DbSgId", value=self.db_sg.security_group_id)

        if ministack_mode:
            # DB endpoint comes from the host-side `ministack-create-db.py` step.
            # Export the subnet IDs so that script knows where to put the
            # DBSubnetGroup it pre-creates.
            CfnOutput(
                self,
                "DbSubnetIds",
                value=",".join(self.db_subnet_ids),
            )
        else:
            CfnOutput(self, "DbHost", value=self.db_cluster.cluster_endpoint.hostname)  # pyright: ignore[reportOptionalMemberAccess]
            CfnOutput(self, "DbPort", value=str(self.db_cluster.cluster_endpoint.port))  # pyright: ignore[reportOptionalMemberAccess]
