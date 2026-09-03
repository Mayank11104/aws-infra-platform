#!/usr/bin/env python3
import os
import sys
import json
import urllib.request
import urllib.error

def post_github_comment(repo: str, pr_number: str, token: str, comment: str):
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    data = {"body": comment}
    
    req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers=headers)
    try:
        with urllib.request.urlopen(req) as response:
            print(f"Successfully posted comment to GitHub PR #{pr_number}")
    except urllib.error.URLError as e:
        print(f"Failed to post comment to GitHub: {e}", file=sys.stderr)

def post_gitlab_comment(project_id: str, mr_iid: str, token: str, comment: str):
    url = f"https://gitlab.com/api/v4/projects/{project_id}/merge_requests/{mr_iid}/notes"
    headers = {
        "PRIVATE-TOKEN": token,
        "Content-Type": "application/json"
    }
    data = {"body": comment}
    
    req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers=headers)
    try:
        with urllib.request.urlopen(req) as response:
            print(f"Successfully posted comment to GitLab MR !{mr_iid}")
    except urllib.error.URLError as e:
        print(f"Failed to post comment to GitLab: {e}", file=sys.stderr)

if __name__ == "__main__":
    # Read comment from stdin or file
    if len(sys.argv) > 1:
        try:
            with open(sys.argv[1], 'r') as f:
                comment = f.read()
        except FileNotFoundError:
            print(f"Error: File '{sys.argv[1]}' not found.", file=sys.stderr)
            sys.exit(1)
    else:
        comment = sys.stdin.read()

    if not comment.strip():
        print("No comment body provided.", file=sys.stderr)
        sys.exit(0)

    # Detect CI environment
    if "GITHUB_ACTIONS" in os.environ or "JENKINS_URL" in os.environ:
        # For Jenkins, we assume it's checking out from GitHub and you pass these as env vars
        repo = os.environ.get("GITHUB_REPOSITORY")
        pr_number = os.environ.get("CHANGE_ID") # Jenkins multi-branch PR ID
        if not pr_number:
            pr_number = os.environ.get("ghprbPullId") # Alternative Jenkins PR plugin
            
        token = os.environ.get("GITHUB_TOKEN")
        
        if repo and pr_number and token:
            post_github_comment(repo, pr_number, token, comment)
        else:
            print("Missing GitHub env vars (GITHUB_REPOSITORY, CHANGE_ID, GITHUB_TOKEN). Skipping comment.", file=sys.stderr)
            # We still print it out so it's visible in the Jenkins console logs!
            print("\n" + "=" * 40)
            print(comment)
            print("=" * 40 + "\n")
            
    elif "GITLAB_CI" in os.environ:
        project_id = os.environ.get("CI_PROJECT_ID")
        mr_iid = os.environ.get("CI_MERGE_REQUEST_IID")
        token = os.environ.get("GITLAB_TOKEN") # Must be provided as a CI/CD variable
        
        if project_id and mr_iid and token:
            post_gitlab_comment(project_id, mr_iid, token, comment)
        else:
            print("Missing GitLab env vars. Skipping comment.", file=sys.stderr)
            print("\n" + "=" * 40)
            print(comment)
            print("=" * 40 + "\n")
            
    else:
        # If running locally or on a standard Jenkins job (not a PR build), just print it.
        print("Not running in a recognized PR environment. Comment printed to stdout instead:")
        print("\n" + "=" * 40)
        print(comment)
        print("=" * 40 + "\n")
