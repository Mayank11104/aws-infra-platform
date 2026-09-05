"""
test_bedrock_keys.py — Quick script to verify IAM credentials can access Bedrock.
"""
import boto3
import json
import os
import sys

def main():
    print("=== AWS Bedrock Credential Tester ===")
    
    # Prompt for credentials
    access_key = input("Enter AWS Access Key ID: ").strip()
    secret_key = input("Enter AWS Secret Access Key: ").strip()
    region = input("Enter AWS Region (e.g., ap-south-1, us-east-1): ").strip()
    
    if not all([access_key, secret_key, region]):
        print("Error: You must provide all three values.")
        sys.exit(1)

    print(f"\nTesting connection to region: {region}...")
    
    # Initialize the Bedrock Runtime client with the provided credentials
    try:
        client = boto3.client(
            service_name='bedrock-runtime',
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key
        )
    except Exception as e:
        print(f"❌ Failed to initialize boto3 client: {e}")
        sys.exit(1)

    model_ids_to_try = [
        "mistral.ministral-3-8b-instruct",
        "mistral.ministral-3-8b-instruct-v0:1",  # Often AWS uses v0:1 for first releases
        "mistral.ministral-3-8b-instruct-v1:0",
        "mistral.ministral-8b-instruct-v0:1",
        "mistral.mistral-7b-instruct-v0:2"
              # Fallback to the older 7B model just to verify keys
    ]
    
    prompt = "<s>[INST] Reply with just the word 'SUCCESS' if you can read this. [/INST]"
    body = json.dumps({
        "prompt": prompt,
        "max_tokens": 50,
        "temperature": 0.0
    })

    print("\nAttempting to invoke models...")
    for model_id in model_ids_to_try:
        print(f"\nTrying: {model_id} ...")
        try:
            response = client.invoke_model(
                modelId=model_id,
                body=body,
                accept='application/json',
                contentType='application/json'
            )
            response_body = json.loads(response.get('body').read())
            output_text = response_body.get('outputs', [{}])[0].get('text', '').strip()
            
            print(f"✅ SUCCESS! This model ID works: {model_id}")
            print(f"🤖 Model replied: {output_text}")
            
            # Update agents/graph.py automatically
            graph_path = os.path.join(os.path.dirname(__file__), 'agents', 'graph.py')
            try:
                with open(graph_path, 'r') as f:
                    content = f.read()
                # Simple replacement for whichever ID it was using
                import re
                content = re.sub(r'mistral\.ministral-[^\"]+', model_id, content)
                with open(graph_path, 'w') as f:
                    f.write(content)
                print("✅ Automatically updated agents/graph.py to use this ID!")
            except Exception as e:
                print(f"⚠️ Could not auto-update graph.py: {e}")
                
            print("\nYou can now safely add your keys to Jenkins!")
            return
            
        except client.exceptions.ValidationException:
            print("❌ Invalid identifier.")
        except client.exceptions.AccessDeniedException:
            print("❌ Access Denied (Check your IAM policy!).")
        except Exception as e:
            print(f"❌ Error: {e}")
            
    print("\nAll attempts failed.")

if __name__ == "__main__":
    main()
