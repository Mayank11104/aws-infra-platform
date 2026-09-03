#!/usr/bin/env python3
import os
import sys
import json
import urllib.request
import urllib.error

# We use the standard urllib to avoid requiring pip install requests,
# making this script ultra-portable in any CI/CD runner.
def ai_review(content: str, context: str) -> str:
    """
    Sends the content to an LLM for review based on the specific context.
    Contexts: 'change_impact', 'terraform_plan', 'ansible_playbook'
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("Warning: OPENAI_API_KEY environment variable not set. Skipping AI review.", file=sys.stderr)
        return "AI Review Skipped: No API key provided."

    # Prompt Templates
    prompts = {
        "change_impact": (
            "You are a DevOps Senior Engineer. I will provide a git diff or list of changed files.\n"
            "Analyze the blast radius of these changes.\n"
            "Identify which environments (dev, staging, prod) are affected by matching the module paths.\n"
            "Output a structured summary with: Files changed, Modules affected, Environments affected, Risk level (LOW/MEDIUM/HIGH), and a brief Recommendation.\n"
            "Keep it very brief and formatting cleanly in Markdown.\n"
        ),
        "terraform_plan": (
            "You are a Cloud Security Architect. Review the following Terraform plan summary.\n"
            "Flag any risky changes such as: 0.0.0.0/0 CIDR blocks, IAM permission changes, or database deletions.\n"
            "Do NOT reject the plan, only provide an advisory Risk Level (LOW/MEDIUM/HIGH) and a brief list of warnings (⚠) with recommendations.\n"
            "Keep it very brief and formatting cleanly in Markdown.\n"
        ),
        "ansible_playbook": (
            "You are a DevOps Automation Expert. Review the following Ansible playbook or role tasks.\n"
            "Flag any anti-patterns such as: using 'shell' or 'command' instead of native modules, missing 'no_log: true' on secrets, or hardcoded passwords.\n"
            "Output an advisory review listing any warnings (⚠) and the specific line/task, along with a recommendation.\n"
            "Keep it very brief and formatting cleanly in Markdown.\n"
        )
    }

    if context not in prompts:
        raise ValueError(f"Unknown review context: {context}")

    system_prompt = prompts[context]
    
    # We use the OpenAI-compatible API format (works for OpenAI, Groq, local LLMs like Ollama, etc)
    # Defaulting to OpenAI's endpoint, but this can be swapped easily via environment variables.
    url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1/chat/completions")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini") # Using mini for speed/cost efficiency

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    data = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Content to review:\n\n{content}"}
        ],
        "temperature": 0.2 # Low temperature for more deterministic, factual reviews
    }

    req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers=headers)
    
    try:
        with urllib.request.urlopen(req) as response:
            result = json.loads(response.read().decode('utf-8'))
            return result['choices'][0]['message']['content']
    except urllib.error.URLError as e:
        print(f"Error communicating with AI API: {e}", file=sys.stderr)
        return f"AI Review Failed: API Error ({e})"
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        return "AI Review Failed: Internal Error"

if __name__ == "__main__":
    # Force UTF-8 encoding for Windows cmd to support emojis
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    if len(sys.argv) < 3:
        print("Usage: python ai_review.py <context> <file_path>")
        sys.exit(1)
        
    context = sys.argv[1]
    file_path = sys.argv[2]
    
    try:
        with open(file_path, 'r') as f:
            content = f.read()
            
        review_output = ai_review(content, context)
        
        # Add a clear header for the PR comment
        header = f"### 🤖 AI {context.replace('_', ' ').title()} Review\n\n"
        print(header + review_output)
        
    except FileNotFoundError:
        print(f"Error: File '{file_path}' not found.", file=sys.stderr)
        sys.exit(1)
