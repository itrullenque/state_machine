#!/usr/bin/env python3
import os

import aws_cdk as cdk

from cdk_app.step_function_stack import TranslationPipelineStack


app = cdk.App()
TranslationPipelineStack(
    app,
    "TranslationPipelineStack",
)

app.synth()
