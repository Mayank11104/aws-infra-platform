"""
bedrock_client.py — Explicit AWS Auth for the LangGraph models.

This file ensures that we NEVER rely on the default boto3 credential chain.
By explicitly requiring BEDROCK_AWS_ACCESS_KEY_ID, we guarantee that the
AI script only authenticates against Account B (the AI account) and can never
accidentally use Account A's (Terraform) credentials, even if they happen to
be present in the Jenkins environment.
"""

import os
import boto3
from langchain_aws import ChatBedrock

def get_bedrock_llm(model_id: str) -> ChatBedrock:
    """
    Constructs a LangChain ChatBedrock instance using explicit credentials.
    Fails loudly if the specific Bedrock credentials are not injected.
    """
    access_key = os.environ.get("BEDROCK_AWS_ACCESS_KEY_ID")
    secret_key = os.environ.get("BEDROCK_AWS_SECRET_ACCESS_KEY")
    
    if not access_key or not secret_key:
        raise ValueError(
            "CRITICAL: Bedrock AWS credentials are not set in the environment. "
            "Ensure the Jenkins pipeline is injecting 'BEDROCK_AWS_ACCESS_KEY_ID' "
            "and 'BEDROCK_AWS_SECRET_ACCESS_KEY' for Account B."
        )

    # Hardcoded region for Bedrock (adjust if you use a different region for Claude models)
    region = os.environ.get("BEDROCK_AWS_REGION", "us-east-1")

    session = boto3.Session(
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
    )
    
    bedrock_runtime = session.client("bedrock-runtime")
    
    # We use temperature=0 for deterministic risk assessment.
    return ChatBedrock(
        client=bedrock_runtime,
        model_id=model_id,
        model_kwargs={"temperature": 0.0}
    )
