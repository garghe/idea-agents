"""CDK stack for the AWS deployment.

  Browser ── CloudFront ──┬── /*      S3 (portal files, private, read through Origin Access Control)
     │                    └── /api/*  API Gateway (Cognito sign-in required) ── Lambdas
     └── Cognito hosted sign-in (optionally federated with the corporate identity provider)

  Lambdas ── Step Functions ── ECS Fargate runner (isolated subnets, no internet) ── Bedrock (Claude)
                                       │
                                       └── S3 (case documents, versioned) + DynamoDB (case records)

  Browser ── signed S3 link ── uploads/ ── GuardDuty malware scan ── EventBridge ── ingest Lambda
                                                                   (text into the case file)

  CloudWatch alarms and an optional AWS Budgets limit ── SNS ── email

The state machine definition is generated from catalogue/pipeline.yaml. The README maps each part
of this stack to the AWS Well-Architected pillars.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import jsii
from aws_cdk import BundlingOptions, CfnOutput, DockerImage, Duration, ILocalBundling, RemovalPolicy, Stack, Tags
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_guardduty as guardduty
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

from idea_agents.aws.asl import Refs, build_definition
from idea_agents.catalogue import Catalogue
from idea_agents.config import PipelineConfig, pipeline_summary
from idea_agents.roles import roles_summary

REPO_ROOT = Path(__file__).resolve().parents[1]
INGEST_DEPS = REPO_ROOT / ".build" / "ingest-deps"  # pip cache for the ingest Lambda (git-ignored)
PYPDF = "pypdf>=5,<8"
CONTAINER_NAME = "runner"
PORTAL_URL_PARAMETER = "/idea/portal-url"
LOG_RETENTION = logs.RetentionDays.SIX_MONTHS


@jsii.implements(ILocalBundling)
class _IngestBundle:
    """Builds the ingest Lambda's code without Docker: the idea_agents package plus pypdf.

    pypdf is pure Python, so a wheel installed here runs on Lambda's Arm runtime as it is.
    The install is cached in .build/ so repeated synths do not download it again.
    """

    def try_bundle(self, output_dir: str, *args, **kwargs) -> bool:
        if not (INGEST_DEPS / "pypdf").is_dir():
            subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--no-deps", "--target",
                            str(INGEST_DEPS), PYPDF], check=True)
        shutil.copytree(INGEST_DEPS, output_dir, dirs_exist_ok=True)
        shutil.copytree(REPO_ROOT / "src" / "idea_agents", Path(output_dir) / "idea_agents", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        return True


class PipelineStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, pipeline: PipelineConfig,
                 model_overrides: dict[str, str] | None = None, auth_domain_prefix: str | None = None,
                 saml_metadata_url: str | None = None, saml_provider_name: str = "CorporateSSO",
                 alarm_email: str | None = None, monthly_budget_usd: float | None = None,
                 cost_centre: str | None = None, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        summary = pipeline_summary(pipeline)

        # ---------------------------------------------------------------- tags (cost allocation)
        Tags.of(self).add("Project", "idea-agents")
        if cost_centre:
            Tags.of(self).add("CostCentre", cost_centre)

        # ---------------------------------------------------------------- alerts
        alerts = sns.Topic(self, "Alerts", display_name="IDEA alerts", enforce_ssl=True)
        alerts.add_to_resource_policy(iam.PolicyStatement(
            principals=[iam.ServicePrincipal("budgets.amazonaws.com")],
            actions=["SNS:Publish"], resources=[alerts.topic_arn]))
        if alarm_email:
            alerts.add_subscription(subs.EmailSubscription(alarm_email))
        alarm_action = cw_actions.SnsAction(alerts)

        def alarm(name: str, metric: cloudwatch.IMetric, description: str) -> None:
            a = cloudwatch.Alarm(
                self, name, metric=metric, threshold=1, evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING, alarm_description=description)
            a.add_alarm_action(alarm_action)

        # ---------------------------------------------------------------- audit logs bucket
        log_bucket = s3.Bucket(
            self, "AccessLogs", enforce_ssl=True, encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.OBJECT_WRITER,  # CloudFront standard logs need ACLs
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(365))],
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ---------------------------------------------------------------- network
        # Isolated subnets: no internet gateway, no NAT. AWS services are reached through endpoints.
        vpc = ec2.Vpc(
            self, "Vpc", max_azs=2, nat_gateways=0,
            subnet_configuration=[ec2.SubnetConfiguration(
                name="isolated", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24)],
        )
        vpc.add_flow_log("FlowLogs", destination=ec2.FlowLogDestination.to_cloud_watch_logs(
            logs.LogGroup(self, "VpcFlowLogs", retention=LOG_RETENTION)))
        vpc.add_gateway_endpoint("S3", service=ec2.GatewayVpcEndpointAwsService.S3)
        vpc.add_gateway_endpoint("DynamoDb", service=ec2.GatewayVpcEndpointAwsService.DYNAMODB)
        for name, service in {
            "Ecr": ec2.InterfaceVpcEndpointAwsService.ECR,  # image pull: registry API
            "EcrDocker": ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,  # image pull: layers (via S3)
            "Logs": ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,  # container logs
            "BedrockRuntime": ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,  # Claude
            "StepFunctions": ec2.InterfaceVpcEndpointAwsService.STEP_FUNCTIONS,  # report back
        }.items():
            vpc.add_interface_endpoint(name, service=service, private_dns_enabled=True)
        runner_sg = ec2.SecurityGroup(
            self, "RunnerSg", vpc=vpc, allow_all_outbound=False,
            description="Agent runner tasks: no inbound; outbound HTTPS to VPC endpoints only")
        runner_sg.add_egress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443),
                                  "HTTPS to the VPC endpoints (no other route exists)")

        # ---------------------------------------------------------------- case file
        bucket = s3.Bucket(
            self, "CaseDocuments", versioned=True, enforce_ssl=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            # Browsers upload attachments straight here with a signed link (PUT only). The signature,
            # not CORS, is what authorises an upload; CORS just lets the portal's page make the call.
            cors=[s3.CorsRule(allowed_methods=[s3.HttpMethods.PUT], allowed_origins=["https://*.cloudfront.net"],
                              allowed_headers=["content-type"], max_age=3000)],
            server_access_logs_bucket=log_bucket, server_access_logs_prefix="case-documents/",
            removal_policy=RemovalPolicy.RETAIN,
        )
        table = dynamodb.Table(
            self, "CaseRecords",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            deletion_protection=True,
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

        # ---------------------------------------------------------------- agent runner (ECS Fargate)
        cluster = ecs.Cluster(self, "Cluster", vpc=vpc, container_insights_v2=ecs.ContainerInsights.ENABLED)
        task_def = ecs.FargateTaskDefinition(
            self, "RunnerTask", cpu=1024, memory_limit_mib=4096,
            runtime_platform=ecs.RuntimePlatform(cpu_architecture=ecs.CpuArchitecture.X86_64,
                                                 operating_system_family=ecs.OperatingSystemFamily.LINUX),
        )
        image = ecr_assets.DockerImageAsset(self, "RunnerImage", directory=str(REPO_ROOT),
                                            platform=ecr_assets.Platform.LINUX_AMD64)
        runner_env = {
            "CASES_BUCKET": bucket.bucket_name,
            "CASES_TABLE": table.table_name,
            "IDEA_USE_BEDROCK": "1",
            "AWS_REGION": self.region,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
            **(model_overrides or {}),
        }
        task_def.add_container(
            CONTAINER_NAME,
            image=ecs.ContainerImage.from_docker_image_asset(image),
            environment=runner_env,
            logging=ecs.LogDrivers.aws_logs(stream_prefix="runner", log_retention=LOG_RETENTION),
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
            "PORTAL_URL_PARAMETER": PORTAL_URL_PARAMETER,
            "PIPELINE_JSON": json.dumps(summary),
        }
        portal_param_arn = self.format_arn(service="ssm", resource="parameter",
                                           resource_name=PORTAL_URL_PARAMETER.lstrip("/"))

        def fn(name: str, module: str, extra_env: dict | None = None, timeout: int = 30,
               writes: bool = True, fn_code: lambda_.Code | None = None, memory: int = 256) -> lambda_.Function:
            f = lambda_.Function(
                self, name, runtime=lambda_.Runtime.PYTHON_3_12, architecture=lambda_.Architecture.ARM_64,
                code=fn_code or code, handler=f"idea_agents.aws.lambdas.{module}.handler",
                environment={**common_env, **(extra_env or {})},
                timeout=Duration.seconds(timeout), memory_size=memory, tracing=lambda_.Tracing.ACTIVE,
                log_group=logs.LogGroup(self, f"{name}Logs", retention=LOG_RETENTION),
            )
            if writes:
                bucket.grant_read_write(f)
                table.grant_read_write_data(f)
                teams_secret.grant_read(f)
                # Read by name, not by reference: referencing the parameter would create a dependency
                # cycle (parameter -> CloudFront -> API -> Lambdas).
                f.add_to_role_policy(iam.PolicyStatement(actions=["ssm:GetParameter"],
                                                         resources=[portal_param_arn]))
            else:  # read-only views
                bucket.grant_read(f)
                table.grant_read_data(f)
            alarm(f"{name}Errors", f.metric_errors(period=Duration.minutes(5)),
                  f"The {name} function returned errors")
            return f

        wait_fn = fn("WaitForPeople", "wait_for_people")
        publish_fn = fn("PublishBacklog", "publish_backlog", {"JIRA_SECRET_ARN": jira_secret.secret_arn}, timeout=300)
        jira_secret.grant_read(publish_fn)
        finish_fn = fn("FinishCase", "finish_case")
        respond_fn = fn("Respond", "respond")
        respond_fn.add_to_role_policy(iam.PolicyStatement(actions=["states:SendTaskSuccess"], resources=["*"]))
        get_fn = fn("GetCase", "get_case", writes=False)
        list_fn = fn("ListCases", "list_cases", writes=False)
        roles_fn = fn("ListRoles", "list_roles", writes=False)
        attach_fn = fn("AttachFiles", "attach")

        # ---------------------------------------------------------------- attachments: scan, then ingest
        # Uploads land under uploads/. GuardDuty Malware Protection for S3 scans each new object and
        # reports the result to EventBridge; the ingest function then extracts the text (or rejects
        # the file). GuardDuty needs this role to manage its own EventBridge rule and read the objects.
        scan_role = iam.Role(self, "UploadScanRole",
                             assumed_by=iam.ServicePrincipal("malware-protection-plan.guardduty.amazonaws.com"),
                             description="GuardDuty Malware Protection for S3: scans attachment uploads")
        gd_rule = f"arn:aws:events:{self.region}:{self.account}:rule/DO-NOT-DELETE-AmazonGuardDutyMalwareProtectionS3*"
        for sid, actions, resources, conditions in (
            ("ManagedRule", ["events:PutRule", "events:DeleteRule", "events:PutTargets", "events:RemoveTargets"],
             [gd_rule], {"StringLike": {"events:ManagedBy": "malware-protection-plan.guardduty.amazonaws.com"}}),
            ("WatchManagedRule", ["events:DescribeRule", "events:ListTargetsByRule"], [gd_rule], None),
            ("TagScanned", ["s3:PutObjectTagging", "s3:GetObjectTagging", "s3:PutObjectVersionTagging",
                            "s3:GetObjectVersionTagging"], [bucket.arn_for_objects("uploads/*")], None),
            ("BucketEvents", ["s3:PutBucketNotification", "s3:GetBucketNotification"], [bucket.bucket_arn], None),
            ("Validation", ["s3:PutObject"], [bucket.arn_for_objects("malware-protection-resource-validation-object")], None),
            ("Ownership", ["s3:ListBucket"], [bucket.bucket_arn], None),
            ("Scan", ["s3:GetObject", "s3:GetObjectVersion"], [bucket.arn_for_objects("uploads/*")], None),
        ):
            scan_role.add_to_policy(iam.PolicyStatement(sid=sid, actions=actions, resources=resources,
                                                        conditions=conditions))
        scan_plan = guardduty.CfnMalwareProtectionPlan(
            self, "UploadScan", role=scan_role.role_arn,
            protected_resource=guardduty.CfnMalwareProtectionPlan.CFNProtectedResourceProperty(
                s3_bucket=guardduty.CfnMalwareProtectionPlan.S3BucketProperty(
                    bucket_name=bucket.bucket_name, object_prefixes=["uploads/"])),
            actions=guardduty.CfnMalwareProtectionPlan.CFNActionsProperty(
                tagging=guardduty.CfnMalwareProtectionPlan.CFNTaggingProperty(status="ENABLED")),
        )
        scan_plan.node.add_dependency(scan_role)  # the role's policy must exist before GuardDuty checks it

        ingest_code = lambda_.Code.from_asset(
            str(REPO_ROOT / "src"), exclude=["**/__pycache__", "*.egg-info", "**/*.pyc"],
            bundling=BundlingOptions(
                image=DockerImage.from_registry("public.ecr.aws/sam/build-python3.12"),
                local=_IngestBundle(),  # used when it can run; Docker is only the fallback
                command=["bash", "-c", f"pip install --no-deps --target /asset-output '{PYPDF}' && "
                                       "cp -r /asset-input/idea_agents /asset-output/"]))
        ingest_fn = fn("IngestAttachment", "ingest", timeout=120, fn_code=ingest_code, memory=1024)
        ingest_dlq = sqs.Queue(self, "IngestDeadLetters", enforce_ssl=True,
                               encryption=sqs.QueueEncryption.SQS_MANAGED, retention_period=Duration.days(14))
        events.Rule(
            self, "UploadScanned",
            description="GuardDuty finished scanning an attachment: ingest it",
            event_pattern=events.EventPattern(
                source=["aws.guardduty"],
                detail_type=["GuardDuty Malware Protection Object Scan Result"],
                detail={"s3ObjectDetails": {"bucketName": [bucket.bucket_name],
                                            "objectKey": [{"prefix": "uploads/"}]}}),
            targets=[targets.LambdaFunction(ingest_fn, dead_letter_queue=ingest_dlq, retry_attempts=4,
                                            max_event_age=Duration.hours(2))],
        )
        alarm("IngestFailed", ingest_dlq.metric_approximate_number_of_messages_visible(period=Duration.minutes(5)),
              "An attachment scan result could not be processed (see the IngestDeadLetters queue)")

        # The roles summary (descriptions, models, budgets) for the portal's Roles page, published from
        # the same catalogue that is built into the runner image. prune=False: never touch case files.
        s3deploy.BucketDeployment(
            self, "RolesSummary",
            sources=[s3deploy.Source.json_data("roles.json", roles_summary(Catalogue(REPO_ROOT / "catalogue")))],
            destination_bucket=bucket, destination_key_prefix="catalogue/", prune=False,
        )

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
        state_machine = sfn.StateMachine(
            self, "Pipeline",
            definition_body=sfn.DefinitionBody.from_string(
                Stack.of(self).to_json_string(build_definition(pipeline, refs))),
            state_machine_type=sfn.StateMachineType.STANDARD,
            tracing_enabled=True,
            logs=sfn.LogOptions(
                destination=logs.LogGroup(self, "PipelineLogs", retention=LOG_RETENTION),
                level=sfn.LogLevel.ERROR,
            ),
        )
        state_machine.add_to_role_policy(iam.PolicyStatement(
            actions=["ecs:RunTask"], resources=[task_def.task_definition_arn],
            conditions={"ArnEquals": {"ecs:cluster": cluster.cluster_arn}}))
        state_machine.add_to_role_policy(iam.PolicyStatement(
            actions=["iam:PassRole"],
            resources=[task_def.task_role.role_arn, task_def.obtain_execution_role().role_arn]))
        for f in (wait_fn, publish_fn, finish_fn):
            f.grant_invoke(state_machine)
        alarm("CasesFailed", state_machine.metric_failed(period=Duration.minutes(5)),
              "A case stopped with an error (see the Step Functions execution named after the case)")
        alarm("CasesTimedOut", state_machine.metric_timed_out(period=Duration.minutes(5)),
              "A case timed out")

        start_fn = fn("StartCase", "start_case", {
            "STATE_MACHINE_ARN": state_machine.state_machine_arn,
            "FIRST_STAGE": pipeline.first_stage().id,
            "MAX_REVIEW_ROUNDS": str(pipeline.limits.max_review_rounds),
        })
        state_machine.grant_start_execution(start_fn)
        # Reopening a rejected case starts a new execution that resumes at the rejected stage.
        respond_fn.add_environment("STATE_MACHINE_ARN", state_machine.state_machine_arn)
        respond_fn.add_environment("MAX_REVIEW_ROUNDS", str(pipeline.limits.max_review_rounds))
        state_machine.grant_start_execution(respond_fn)

        # ---------------------------------------------------------------- sign-in (Cognito)
        user_pool = cognito.UserPool(
            self, "Users", self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True),
                fullname=cognito.StandardAttribute(required=False, mutable=True)),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            password_policy=cognito.PasswordPolicy(min_length=12),
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(sms=False, otp=True),
            removal_policy=RemovalPolicy.RETAIN,
        )
        providers = [cognito.UserPoolClientIdentityProvider.COGNITO]
        saml = None
        if saml_metadata_url:
            saml = cognito.UserPoolIdentityProviderSaml(
                self, "CorporateSso", user_pool=user_pool, name=saml_provider_name,
                metadata=cognito.UserPoolIdentityProviderSamlMetadata.url(saml_metadata_url),
                attribute_mapping=cognito.AttributeMapping(
                    email=cognito.ProviderAttribute.other(
                        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"),
                    fullname=cognito.ProviderAttribute.other(
                        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name")),
            )
            providers.append(cognito.UserPoolClientIdentityProvider.custom(saml_provider_name))
        domain = user_pool.add_domain(
            "Domain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=auth_domain_prefix or f"idea-{self.account}"),
            managed_login_version=cognito.ManagedLoginVersion.CLASSIC_HOSTED_UI,  # works without branding setup
        )

        # ---------------------------------------------------------------- API (Cognito sign-in required)
        authorizer = apigw.CognitoUserPoolsAuthorizer(self, "Authorizer", cognito_user_pools=[user_pool])
        api_access_logs = logs.LogGroup(self, "ApiAccessLogs", retention=LOG_RETENTION)
        rest_api = apigw.RestApi(
            self, "Api", rest_api_name="idea-agents", cloud_watch_role=True,
            default_method_options=apigw.MethodOptions(
                authorization_type=apigw.AuthorizationType.COGNITO, authorizer=authorizer),
            deploy_options=apigw.StageOptions(
                stage_name="prod", throttling_rate_limit=20, throttling_burst_limit=40,
                tracing_enabled=True,
                access_log_destination=apigw.LogGroupLogDestination(api_access_logs),
                access_log_format=apigw.AccessLogFormat.json_with_standard_fields(
                    caller=False, http_method=True, ip=True, protocol=True, request_time=True,
                    resource_path=True, response_length=True, status=True, user=True)),
        )
        api_root = rest_api.root.add_resource("api")
        api_root.add_resource("roles").add_method("GET", apigw.LambdaIntegration(roles_fn))
        cases = api_root.add_resource("cases")
        cases.add_method("GET", apigw.LambdaIntegration(list_fn))
        cases.add_method("POST", apigw.LambdaIntegration(start_fn))
        one = cases.add_resource("{case_id}")
        one.add_method("GET", apigw.LambdaIntegration(get_fn))
        one.add_resource("respond").add_method("POST", apigw.LambdaIntegration(respond_fn))
        one.add_resource("attachments").add_method("POST", apigw.LambdaIntegration(attach_fn))
        alarm("ApiServerErrors", rest_api.metric_server_error(period=Duration.minutes(5)),
              "The API returned 5xx errors")

        # ---------------------------------------------------------------- portal (S3 + CloudFront)
        portal_bucket = s3.Bucket(
            self, "PortalFiles", enforce_ssl=True, encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.DESTROY, auto_delete_objects=True,
        )
        distribution = cloudfront.Distribution(
            self, "Portal",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(portal_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                response_headers_policy=cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
            ),
            additional_behaviors={
                "/api/*": cloudfront.BehaviorOptions(
                    origin=origins.RestApiOrigin(rest_api),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                ),
            },
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            enable_logging=True, log_bucket=log_bucket, log_file_prefix="cloudfront/",
        )
        portal_url = f"https://{distribution.distribution_domain_name}"

        client = user_pool.add_client(
            "PortalClient", generate_secret=False,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
                callback_urls=[portal_url + "/"], logout_urls=[portal_url + "/"]),
            supported_identity_providers=providers,
            id_token_validity=Duration.hours(1), access_token_validity=Duration.hours(1),
            prevent_user_existence_errors=True,
        )
        if saml:
            client.node.add_dependency(saml)

        s3deploy.BucketDeployment(
            self, "PortalDeploy",
            sources=[
                s3deploy.Source.asset(str(REPO_ROOT / "portal"), exclude=["config.json"]),
                s3deploy.Source.json_data("config.json", {
                    "mode": "aws",
                    "apiBase": "/api",
                    "pipeline": summary,
                    "auth": {"baseUrl": domain.base_url(), "clientId": client.user_pool_client_id,
                             "redirectUri": portal_url + "/"},
                }),
            ],
            destination_bucket=portal_bucket,
            distribution=distribution,
            distribution_paths=["/*"],
        )
        ssm.StringParameter(self, "PortalUrlParameter", parameter_name=PORTAL_URL_PARAMETER,
                            string_value=portal_url, description="Portal address used in notification links")

        # ---------------------------------------------------------------- AWS Budgets (optional)
        if monthly_budget_usd:
            budgets.CfnBudget(
                self, "MonthlyBudget",
                budget=budgets.CfnBudget.BudgetDataProperty(
                    budget_name=f"idea-{self.stack_name}-monthly",
                    budget_type="COST", time_unit="MONTHLY",
                    budget_limit=budgets.CfnBudget.SpendProperty(amount=monthly_budget_usd, unit="USD")),
                notifications_with_subscribers=[
                    budgets.CfnBudget.NotificationWithSubscribersProperty(
                        notification=budgets.CfnBudget.NotificationProperty(
                            notification_type=kind, comparison_operator="GREATER_THAN",
                            threshold=threshold, threshold_type="PERCENTAGE"),
                        subscribers=[budgets.CfnBudget.SubscriberProperty(
                            subscription_type="SNS", address=alerts.topic_arn)])
                    for kind, threshold in (("ACTUAL", 80), ("FORECASTED", 100))
                ],
            )

        CfnOutput(self, "PortalUrl", value=portal_url)
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "SignInDomain", value=domain.base_url())
        CfnOutput(self, "StateMachineArn", value=state_machine.state_machine_arn)
        CfnOutput(self, "CaseBucket", value=bucket.bucket_name)
        CfnOutput(self, "CaseTable", value=table.table_name)
        CfnOutput(self, "AlertsTopic", value=alerts.topic_arn)
        CfnOutput(self, "TeamsWebhookSecret", value=teams_secret.secret_name)
        CfnOutput(self, "JiraSecret", value=jira_secret.secret_name)
