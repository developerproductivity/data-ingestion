#!/usr/bin/env python3
import argparse
import json
import os
import requests
import sys
import time
from datetime import datetime
from typing import List, Dict, Any, Optional

# Import the upload function from app.py
from app import upload_ci_build_data

def configure_parser() -> argparse.ArgumentParser:
    """Configure command line argument parser."""
    parser = argparse.ArgumentParser(
        description="Upload CI build data for old PRs in a given repository to Logilica"
    )
    parser.add_argument(
        "--repo", 
        required=True,
        help="Repository name in format 'owner/repo'"
    )
    parser.add_argument(
        "--token", 
        help="GitHub token with repo access. Can also set GITHUB_TOKEN env variable."
    )
    parser.add_argument(
        "--start-pr", 
        type=int, 
        default=1,
        help="Starting PR number to process (default: 1)"
    )
    parser.add_argument(
        "--end-pr", 
        type=int, 
        help="Ending PR number to process (default: latest PR)"
    )
    parser.add_argument(
        "--ci-context", 
        default="ci/prow/e2e",
        help="CI context to search for (default: ci/prow/e2e)"
    )
    return parser

def get_github_token(args) -> str:
    """Get GitHub token from args or environment."""
    token = args.token or os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("Error: GitHub token required. Pass --token or set GITHUB_TOKEN env var")
    return token

def get_pr_data(repo: str, pr_number: int, token: str) -> Optional[Dict[str, Any]]:
    """Fetch PR data from GitHub API."""
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"token {token}",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"Error fetching PR {pr_number}: {e}")
        return None

def get_pr_status(repo: str, commit_sha: str, token: str, context: str) -> Optional[Dict[str, Any]]:
    """Fetch status checks for a commit, filtering by context."""
    url = f"https://api.github.com/repos/{repo}/commits/{commit_sha}/status"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"token {token}",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        
        # Find the specific context we're looking for
        for status in data.get('statuses', []):
            if status.get('context').startswith(context):
                return status
        
        return None
    except requests.RequestException as e:
        print(f"Error fetching status for commit {commit_sha}: {e}")
        return None

def fetch_gcs_json(gcs_path: str) -> Optional[Dict[str, Any]]:
    """Fetch JSON data from Google Cloud Storage."""
    # Extract bucket and object path from gs:// URL
    if not gcs_path.startswith("gs://"):
        print(f"Invalid GCS path: {gcs_path}")
        return None
    
    path_parts = gcs_path[5:].split('/', 1)
    if len(path_parts) < 2:
        print(f"Invalid GCS path format: {gcs_path}")
        return None
    
    bucket_name = path_parts[0]
    source_blob_name = path_parts[1]
    
    try:
        # Use anonymous client to access public GCS buckets
        from google.cloud import storage
        storage_client = storage.Client.create_anonymous_client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(source_blob_name)
        data = blob.download_as_bytes()
        return json.loads(data.decode('utf-8'))
    except Exception as e:
        print(f"Error downloading from GCS: {str(e)}")
        return None

def process_pr(repo: str, pr_number: int, token: str, context: str) -> bool:
    """Process a single PR and upload its CI data if available."""
    print(f"Processing PR #{pr_number}...")
    
    # Get PR data
    pr_data = get_pr_data(repo, pr_number, token)
    if not pr_data:
        print(f"Skipping PR #{pr_number}: Could not fetch PR data")
        return False
    
    # Get the head commit SHA
    head_sha = pr_data.get('head', {}).get('sha')
    if not head_sha:
        print(f"Skipping PR #{pr_number}: No head commit found")
        return False
    
    # Get status for this commit with the specified context
    status = get_pr_status(repo, head_sha, token, context)
    if not status:
        print(f"Skipping PR #{pr_number}: No '{context}' status found")
        return False
    
    if status.get('state') not in ('success', 'failure'):
        print(f"Skipping PR #{pr_number}: Status is '{status.get('state')}', need success or failure")
        return False
    
    # Get the target URL which should point to GCS data
    target_url = status.get('target_url')
    if not target_url or "/gs/" not in target_url:
        print(f"Skipping PR #{pr_number}: Invalid target URL")
        return False
    
    # Extract GCS paths
    gcs_path = target_url.split("/gs/")[-1]
    bucket_name = gcs_path.split("/", 1)[0]
    file_prefix = gcs_path.split("/", 1)[1]
    
    # Fetch finished.json and started.json
    finished_json = fetch_gcs_json(f"gs://{bucket_name}/{file_prefix}/finished.json")
    started_json = fetch_gcs_json(f"gs://{bucket_name}/{file_prefix}/started.json")
    
    if not finished_json or not started_json:
        print(f"Skipping PR #{pr_number}: Missing JSON data")
        return False
    
    # Get author information
    triggered_name = pr_data.get('user', {}).get('name') or pr_data.get('user', {}).get('login')
    triggered_email = pr_data.get('user', {}).get('email') or f"{triggered_name}@users.noreply.github.com"
    triggered_id = pr_data.get('user', {}).get('login')
    
    # Upload data
    try:
        upload_ci_build_data(
            target_url, 
            finished_json, 
            started_json, 
            triggered_name, 
            triggered_email, 
            triggered_id
        )
        print(f"✓ Successfully uploaded CI data for PR #{pr_number}")
        return True
    except Exception as e:
        print(f"✗ Failed to upload CI data for PR #{pr_number}: {str(e)}")
        return False

def main():
    parser = configure_parser()
    args = parser.parse_args()
    
    token = get_github_token(args)
    repo = args.repo
    start_pr = args.start_pr
    end_pr = args.end_pr
    context = args.ci_context
    
    # If end_pr not specified, get the latest PR number
    if not end_pr:
        latest_prs_url = f"https://api.github.com/repos/{repo}/pulls?per_page=1&state=all"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"token {token}",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        
        try:
            response = requests.get(latest_prs_url, headers=headers)
            response.raise_for_status()
            prs = response.json()
            if prs:
                end_pr = prs[0]["number"]
            else:
                sys.exit("Error: Could not determine latest PR number")
        except requests.RequestException as e:
            sys.exit(f"Error fetching latest PR: {e}")
    
    print(f"Processing PRs #{start_pr} to #{end_pr} for {repo}")
    
    success_count = 0
    total_count = 0
    
    for pr_num in range(start_pr, end_pr + 1):
        total_count += 1
        if process_pr(repo, pr_num, token, context):
            success_count += 1
        
        # Add a small delay to avoid rate limiting
        time.sleep(0.5)
    
    print(f"\nCompleted processing {total_count} PRs")
    print(f"Successfully uploaded data for {success_count} PRs")
    print(f"Failed to upload data for {total_count - success_count} PRs")

if __name__ == "__main__":
    main() 