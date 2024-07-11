'''
Defining Event patterns: https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-event-patterns.html
'''

import aws_cdk as cdk
from aws_cdk import (
    Stack,
    aws_s3 as s3,
    aws_stepfunctions as sfn,
    aws_iam as iam,
    aws_events as events,
    aws_events_targets as targets,
    aws_logs as logs,
)
from constructs import Construct


class TranslationPipelineStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Create S3 bucket with lifecycle policy to delete objects
        bucket = s3.Bucket(
            self,
            "AudioBucketNew",
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            event_bridge_enabled=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # Create IAM role for Step Functions
        sfn_role = iam.Role(
            self,
            "StepFunctionsRole",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
        )

        # Grant permissions to the role
        bucket.grant_read_write(sfn_role)
        sfn_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonTranscribeFullAccess")
        )
        sfn_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("TranslateFullAccess")
        )
        sfn_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonPollyFullAccess")
        )
        sfn_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonS3ReadOnlyAccess")
        )

        # Create a CloudWatch Log Group for Step Functions
        log_group = logs.LogGroup(
            self,
            "TranslationStateMachineLogGroup",
            retention=logs.RetentionDays.ONE_WEEK,
        )

        state_machine = sfn.StateMachine(
            self, 'TranslationStateMachine',
            state_machine_name = 'TranslatorMachine',
            role=sfn_role,
            logs={"destination": log_group, "level": sfn.LogLevel.ALL},
            definition_body=sfn.DefinitionBody.from_file(
                "./assets/state_machine_definition.json"
            )
        )

        rule = events.Rule(
            self, "S3UploadRule",
            event_pattern={
                "source": ["aws.s3"],
                "detail_type": ["Object Created"],
                "detail": {
                    "bucket": {"name": [bucket.bucket_name]},
                    "object": {
                        "key":[
                            {"suffix":".mp3"},
                            {"suffix":".mp4"},
                        ]
                    }
                }
            }
        )

        #target the rule
        rule.add_target(targets.SfnStateMachine(state_machine))

        #cloudwatch logs for debugging
        rule.add_target(
            targets.CloudWatchLogGroup(
                logs.LogGroup(
                    self,
                    "EventBridgeDebugLogGroup",
                    retention=logs.RetentionDays.ONE_WEEK,
                )
            )
        )

        #output the S3 Bucket Name
        cdk.CfnOutput(
            self,
            "S3BucketName",
            value=bucket.bucket_name,
            description="S3 bucket name",
        )
