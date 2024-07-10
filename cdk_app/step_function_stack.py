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

        # Define Step Functions tasks
        start_transcription_job = sfn.CustomState(
            self,
            "StartTranscriptionJob",
            state_json={
                "Type": "Task",
                "Resource": "arn:aws:states:::aws-sdk:transcribe:startTranscriptionJob",
                "Parameters": {
                    "TranscriptionJobName.$": "States.Format('TranscriptionJob-{}', $$.Execution.Name)",
                    "IdentifyLanguage": True,
                    "Media": {
                        "MediaFileUri.$": "States.Format('s3://{}/{}', $.detail.bucket.name, $.detail.object.key)"
                    },
                    "OutputBucketName.$": "$.detail.bucket.name",
                },
                "ResultPath": "$.TranscriptionJobDetails",
            },
        )

        get_transcription_job = sfn.CustomState(
            self,
            "GetTranscriptionJob",
            state_json={
                "Type": "Task",
                "Resource": "arn:aws:states:::aws-sdk:transcribe:getTranscriptionJob",
                "Parameters": {
                    "TranscriptionJobName.$": "$.TranscriptionJobDetails.TranscriptionJob.TranscriptionJobName"
                },
                "ResultPath": "$.TranscriptionJobDetails",
            },
        )

        get_transcription_file = sfn.CustomState(
            self,
            "GetTranscriptionFile",
            state_json={
                "Type": "Task",
                "Resource": "arn:aws:states:::aws-sdk:s3:getObject",
                "Parameters": {
                    "Bucket.$": "States.ArrayGetItem(States.StringSplit($.TranscriptionJobDetails.TranscriptionJob.Transcript.TranscriptFileUri, '/'), 2)",
                    "Key.$": "States.ArrayGetItem(States.StringSplit($.TranscriptionJobDetails.TranscriptionJob.Transcript.TranscriptFileUri, '/'), 3)",
                },
                "ResultSelector": {"FileContents.$": "States.StringToJson($.Body)"},
                "ResultPath": "$.TranscriptionFile",
            },
        )

        wait_state = sfn.Wait(
            self,
            "WaitForTranscription",
            time=sfn.WaitTime.duration(cdk.Duration.seconds(45)),
        )

        translate_text = sfn.CustomState(
            self,
            "TranslateText",
            state_json={
                "Type": "Task",
                "Resource": "arn:aws:states:::aws-sdk:translate:translateText",
                "Parameters": {
                    "SourceLanguageCode.$": "$.TranscriptionJobDetails.TranscriptionJob.LanguageCode",
                    "TargetLanguageCode": "en",
                    "Text.$": "$.TranscriptionFile.FileContents.results.transcripts[0].transcript",
                },
                "ResultPath": "$.TranslatedText",
            },
        )

        synthesize_speech = sfn.CustomState(
            self,
            "SynthesizeSpeech",
            state_json={
                "Type": "Task",
                "Resource": "arn:aws:states:::aws-sdk:polly:startSpeechSynthesisTask",
                "Parameters": {
                    "OutputFormat": "mp3",
                    "OutputS3BucketName.$": "$.detail.bucket.name",
                    "OutputS3KeyPrefix": "translations/",
                    "Text.$": "$.TranslatedText.TranslatedText",
                    "VoiceId": "Joanna",
                },
            },
        )

        # Define Choice state to determine if the language is English
        is_language_english = sfn.Choice(self, "IsLanguageEnglish?")
        is_language_english.when(
            sfn.Condition.string_matches(
                "$.TranscriptionJobDetails.TranscriptionJob.LanguageCode", "en*"
            ),
            sfn.Pass(self, "SkipTranslation"),
        ).otherwise(get_transcription_file.next(translate_text).next(synthesize_speech))

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
                    is_language_english,
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
