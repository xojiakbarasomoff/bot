import subprocess
import sys

def run(cmd):
    print(f"Running: {cmd}")
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    print("STDOUT:", res.stdout)
    print("STDERR:", res.stderr)
    return res.returncode

def main():
    repo_url = "git@github.com:xojiakbarasomoff/project01.git"
    branch_name = "telegram"
    
    # Switch to or create 'telegram' branch
    code = run(f"git checkout -b {branch_name}")
    if code != 0:
        run(f"git checkout {branch_name}")
        
    # Push to specified remote repository and branch
    push_code = run(f"git push {repo_url} {branch_name}")
    if push_code == 0:
        print("SUCCESS: Pushed to", repo_url, "branch", branch_name)
    else:
        print("FAILURE: Could not push to remote repository.")

if __name__ == "__main__":
    main()
