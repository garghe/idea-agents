"""CDK stack for the AWS MVP.

  API Gateway (IAM auth) ──> Lambdas ──> Step Functions ──> ECS Fargate runner ──> Bedrock (Claude)
                                             │                    │
                                             └──── S3 (documents) + DynamoDB (records)

The runner has no internet access: it sits in isolated subnets and reaches AWS services through
VPC endpoints. The state machine definition is generated from catalogue/pipeline.yaml.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

from agentpipe.aws.asl import Refs, build_definition
from agentpipe.config import PipelineConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTAINER_NAME = "runner"


class PipelineStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, pipeline: PipelineConfig,
                 portal_url: str = "", model_overrides: dict[str, str] | None = None, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---------------------------------------------------------------- network
        vpc = ec2.Vpc(
            self, "Vpc", max_azs=2, nat_gateways=0,
            subnet_configuration=[ec2.SubnetConfiguration(
                name="isolated", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24)],
        )
        vpc.add_gateway_endpoint("S3", service=ec2.GatewayVpcEndpointAwsService.S3)
        vpc.add_gateway_endpoint("DynamoDb", service=ec2.GatewayVpcEndpointAwsService.DYNAMODB)
        for name, service in {
            "Ecr": ec2.InterfaceVpcEndpointAwsService.ECR,
            "EcrDocker": ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
            "Logs": ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
            "BedrockRuntime": ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
            "StepFunctions": ec2.InterfaceVpcEndpointAwsService.STEP_FUNCTIONS,
        }.items():
            vpc.add_interface_endpoint(name, service=service, private_dns_enabled=True)
        runner_sg = ec2.SecurityGroup(self, "RunnerSg", vpc=vpc,
                                      description="Agent runner tasks: outbound to VPC endpoints only")

        # ---------------------------------------------------------------- case file
        bucket = s3.Bucket(
            self, "CaseDocuments", versioned=True, enforce_ssl=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.RETAIN,
        )
        table = dynamodb.Table(
            self, "CaseRecords",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ---------------------------------------------------------------- secrets (filled in by hand)
        teams_secret = secretsmanager.Secret(
            self, "TeamsWebhook",
            description='Teams incoming webhook as JSON: {"url": "https://..."}. Leave as is to disable.')
        jira_secret = secretsmanager.Secret(
            self, "Jira",
            description='Jira as JSON: {"base_url", "email", "api_token", "project_key"}. '
                        'Leave as is for dry-run publishing.')

        # ---------------------------------------------------------------- agent runner
        cluster = ecs.Cluster(self, "Cluster", vpc=vpc, container_insights_v2=ecs.ContainerInsights.ENABLED)
        task_def = ecs.FargateTaskDefinition(self, "RunnerTask", cpu=1024, memory_limit_mib=4096)
        image = ecr_assets.DockerImageAsset(self, "RunnerImage", directory=str(REPO_ROOT),
                                            platform=ecr_assets.Platform.LINUX_AMD64)
        runner_env = {
            "CASES_BUCKET": bucket.bucket_name,
            "CASES_TABLE": table.table_name,
            "AGENTPIPE_USE_BEDROCK": "1",
            "AWS_REGION": self.region,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
            **(model_overrides or {}),
        }
        task_def.add_container(
            CONTAINER_NAME,
            image=ecs.ContainerImage.from_docker_image_asset(image),
            environment=runner_env,
            logging=ecs.LogDrivers.aws_logs(stream_prefix="runner", log_retention=logs.RetentionDays.SIX_MONTHS),
        )
        bucket.grant_read_write(task_def.task_role)
        table.grant_read_write_data(task_def.task_role)
        task_def.task_role.add_to_principal_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=[
                "arn:aws:bedrock:*::foundation-model/anthropic.*",
                f"arn:aws:bedrock:*:{self.account}:inference-profile/*",
                f"arn:aws:bedrock:*:{self.account}:application-inference-profile/*",
            ],
        ))
        task_def.task_role.add_to_principal_policy(iam.PolicyStatement(
            actions=["states:SendTaskSuccess", "states:SendTaskFailure", "states:SendTaskHeartbeat"],
            resources=["*"],  # task tokens are not resource-scoped
        ))

        # ---------------------------------------------------------------- lambdas
        code = lambda_.Code.from_asset(str(REPO_ROOT / "src"),
                                       exclude=["**/__pycache__", "*.egg-info", "**/*.pyc"])
        common_env = {
            "CASES_BUCKET": bucket.bucket_name,
            "CASES_TABLE": table.table_name,
            "TEAMS_WEBHOOK_SECRET_ARN": teams_secret.secret_arn,
            "PORTAL_URL": portal_url,
        }

        def fn(name: str, module: str, extra_env: dict | None = None, timeout: int = 30) -> lambda_.Function:
            f = lambda_.Function(
                self, name, runtime=lambda_.Runtime.PYTHON_3_12, code=code,
                handler=f"agentpipe.aws.lambdas.{module}.handler",
                environment={**common_env, **(extra_env or {})},
                timeout=Duration.seconds(timeout), memory_size=256,
                log_group=logs.LogGroup(self, f"{name}Logs", retention=logs.RetentionDays.SIX_MONTHS),
            )
            bucket.grant_read_write(f)
            table.grant_read_write_data(f)
            teams_secret.grant_read(f)
            return f

        wait_fn = fn("WaitForPeople", "wait_for_people")
        publish_fn = fn("PublishBacklog", "publish_backlog", {"JIRA_SECRET_ARN": jira_secret.secret_arn}, timeout=300)
        jira_secret.grant_read(publish_fn)
        finish_fn = fn("FinishCase", "finish_case")
        respond_fn = fn("Respond", "respond")
        respond_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["states:SendTaskSuccess"], resources=["*"]))
        get_fn = fn("GetCase", "get_case")

        # ---------------------------------------------------------------- state machine
        refs = Refs(
            cluster_arn=cluster.cluster_arn,
            task_definition_arn=task_def.task_definition_arn,
            subnet_ids=tuple(s.subnet_id for s in vpc.isolated_subnets),
            security_group_ids=(runner_sg.security_group_id,),
            container_name=CONTAINER_NAME,
            wait_function_arn=wait_fn.function_arn,
            publish_function_arn=publish_fn.function_arn,
            finish_function_arn=finish_fn.function_arn,
        )
        definition = build_definition(pipeline, refs)
        state_machine = sfn.StateMachine(
            self, "Pipeline",
            definition_body=sfn.DefinitionBody.from_string(Stack.of(self).to_json_string(definition)),
            state_machine_type=sfn.StateMachineType.STANDARD,
            tracing_enabled=True,
            logs=sfn.LogOptions(
                destination=logs.LogGroup(self, "PipelineLogs", retention=logs.RetentionDays.SIX_MONTHS),
                level=sfn.LogLevel.ERROR,
            ),
        )
        state_machine.add_to_role_policy(iam.PolicyStatement(
            actions=["ecs:RunTask"], resources=[task_def.task_definition_arn]))
        state_machine.add_to_role_policy(iam.PolicyStatement(
            actions=["iam:PassRole"],
            resources=[task_def.task_role.role_arn, task_def.obtain_execution_role().role_arn]))
        for f in (wait_fn, publish_fn, finish_fn):
            f.grant_invoke(state_machine)

        start_fn = fn("StartCase", "start_case", {
            "STATE_MACHINE_ARN": state_machine.state_machine_arn,
            "FIRST_STAGE": pipeline.first_stage().id,
            "MAX_REVIEW_ROUNDS": str(pipeline.limits.max_review_rounds),
        })
        state_machine.grant_start_execution(start_fn)

        # ---------------------------------------------------------------- API (IAM auth)
        api = apigw.RestApi(
            self, "Api", rest_api_name="agent-pipeline",
            default_method_options=apigw.MethodOptions(authorization_type=apigw.AuthorizationType.IAM),
            deploy_options=apigw.StageOptions(throttling_rate_limit=10, throttling_burst_limit=20),
        )
        cases = api.root.add_resource("cases")
        cases.add_method("POST", apigw.LambdaIntegration(start_fn))
        one = cases.add_resource("{case_id}")
        one.add_method("GET", apigw.LambdaIntegration(get_fn))
        one.add_resource("respond").add_method("POST", apigw.LambdaIntegration(respond_fn))

        CfnOutput(self, "ApiUrl", value=api.url)
        CfnOutput(self, "StateMachineArn", value=state_machine.state_machine_arn)
        CfnOutput(self, "CaseBucket", value=bucket.bucket_name)
        CfnOutput(self, "CaseTable", value=table.table_name)
        CfnOutput(self, "TeamsWebhookSecret", value=teams_secret.secret_name)
        CfnOutput(self, "JiraSecret", value=jira_secret.secret_name)
