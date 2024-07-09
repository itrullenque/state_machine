import aws_cdk as cdk
from aws_cdk import (
    Stack,
    aws_s3 as s3,
    aws_s3_notifications as s3n,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as tasks,
    aws_iam as iam,
    aws_events as events,
    aws_events_targets as targets,
    aws_logs as logs,
    aws_cloudtrail as cloudtrail,
)
from constructs import Construct


class TranslationPipelineStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Create S3 bucket
        bucket = s3.Bucket(
            self,
            "AudioBucketNew",
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            event_bridge_enabled=True,
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

        # Define Step Functions tasks
        start_transcription_job = tasks.CallAwsService(
            self,
            "StartTranscriptionJob",
            service="transcribe",
            action="startTranscriptionJob",
            parameters={
                "TranscriptionJobName.$": "States.Format('TranscriptionJob-{}', $$.Execution.Name)",
                "IdentifyLanguage": True,
                "Media": {
                    "MediaFileUri.$": "States.Format('s3://{}/{}', $.detail.bucket.name, $.detail.object.key)"
                },
                "OutputBucketName.$": "$.detail.bucket.name",
            },
            iam_resources=["*"],
            result_path="$.TranscriptionJobDetails",
        )

        get_transcription_job = tasks.CallAwsService(
            self,
            "GetTranscriptionJob",
            service="transcribe",
            action="getTranscriptionJob",
            parameters={
                "TranscriptionJobName.$": "$.TranscriptionJobDetails.TranscriptionJob.TranscriptionJobName"
            },
            iam_resources=["*"],
            result_path="$.TranscriptionJobDetails",
        )

        wait_state = sfn.Wait(
            self,
            "WaitForTranscription",
            time=sfn.WaitTime.duration(cdk.Duration.seconds(45)),
        )

        get_transcription_file = tasks.CallAwsService(
            self,
            "GetTranscriptionFile",
            service="s3",
            action="getObject",
            parameters={
                "Bucket.$": "States.ArrayGetItem(States.StringSplit($.TranscriptionJobDetails.TranscriptionJob.Transcript.TranscriptFileUri, '/'), 2)",
                "Key.$": "States.ArrayGetItem(States.StringSplit($.TranscriptionJobDetails.TranscriptionJob.Transcript.TranscriptFileUri, '/'), 3)",
            },
            iam_resources=["*"],
            result_selector={"FileContents.$": "States.StringToJson($.Body)"},
            result_path="$.TranscriptionFile",
        )

        translate_text = tasks.CallAwsService(
            self,
            "TranslateText",
            service="translate",
            action="translateText",
            parameters={
                "SourceLanguageCode.$": "$.TranscriptionJobDetails.TranscriptionJob.LanguageCode",
                "TargetLanguageCode": "en",
                "Text.$": "$.TranscriptionFile.FileContents.results.transcripts[0].transcript",
            },
            iam_resources=["*"],
            result_path="$.TranslatedText",
        )

        synthesize_speech = tasks.CallAwsService(
            self,
            "SynthesizeSpeech",
            service="polly",
            action="startSpeechSynthesisTask",
            parameters={
                "OutputFormat": "mp3",
                "OutputS3BucketName.$": "$.detail.bucket.name",
                "OutputS3KeyPrefix": "translations/",
                "Text.$": "$.TranslatedText.TranslatedText",
                "VoiceId": "Joanna",
            },
            iam_resources=["*"],
        )

        definition = (
            start_transcription_job.next(wait_state)
            .next(get_transcription_job)
            .next(
                sfn.Choice(self, "TranscriptionComplete?")
                .when(
                    sfn.Condition.string_equals(
                        "$.TranscriptionJobDetails.TranscriptionJob.TranscriptionJobStatus",
                        "COMPLETED",
                    ),
                    get_transcription_file.next(translate_text).next(synthesize_speech),
                )
                .otherwise(wait_state)
            )
        )

        state_machine = sfn.StateMachine(
            self,
            "TranslationStateMachineNew",
            definition=definition,
            role=sfn_role,
            logs={"destination": log_group, "level": sfn.LogLevel.ALL},
        )

        # Create EventBridge rule
        rule = events.Rule(
            self,
            "S3UploadRule",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={
                    "bucket": {"name": [bucket.bucket_name]},
                    "object": {
                        "key": [
                            {"suffix": ".mp4"},
                        ]
                    },
                },
            ),
        )

        # Add target to the rule
        rule.add_target(targets.SfnStateMachine(state_machine))

        # Add CloudWatch Logs as a target for debugging
        rule.add_target(
            targets.CloudWatchLogGroup(
                logs.LogGroup(
                    self,
                    "EventBridgeDebugLogGroup",
                    retention=logs.RetentionDays.ONE_WEEK,
                )
            )
        )

        # Output the State Machine ARN
        cdk.CfnOutput(
            self,
            "StateMachineARN",
            value=state_machine.state_machine_arn,
            description="State Machine ARN",
        )

        # Output the S3 Bucket Name
        cdk.CfnOutput(
            self,
            "S3BucketName",
            value=bucket.bucket_name,
            description="S3 bucket name",
        )

        # Output the CloudWatch Log Group Name
        cdk.CfnOutput(
            self,
            "LogGroupName",
            value=log_group.log_group_name,
            description="CloudWatch Log Group",
        )
