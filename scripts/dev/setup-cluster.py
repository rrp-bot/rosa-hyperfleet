#!/usr/bin/env python3
"""
setup-cluster.py — Provision a ROSA HyperFleet hosted cluster in an ephemeral environment.

Usage:
    python3 scripts/dev/setup-cluster.py <ephemeral-id> <cluster-name>

Example:
    python3 scripts/dev/setup-cluster.py 2d5e9171 psav-eventbridge

Config (all overridable via environment variables):
    CUST_PROFILE   AWS profile for the customer account  (default: rrp-ephemeral-customer)
    RC_PROFILE     AWS profile for the RC account        (default: rrp-ephemeral-rc)
    ENVS_FILE      Path to the .ephemeral-envs file      (default: .ephemeral-envs)
    POLL_INTERVAL  Seconds between cluster list polls    (default: 15)
    POLL_TIMEOUT   Total seconds before giving up        (default: 600)

Steps:
    1. Parse .ephemeral-envs to resolve API_URL and REGION for the given ephemeral ID.
    2. Derive ACCOUNT_ID via sts get-caller-identity on the customer profile.
    3. (parallel) cluster-iam create + cluster-vpc create  [customer account]
    4. Register the customer account with the Platform API  [RC account, SigV4 curl]
    5. rosactl login + cluster create                       [customer account]
    6. Poll cluster list until oidc_issuer_url is present   [customer account]
    7. cluster-oidc create                                  [customer account]
"""

import json
import os
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CUST_PROFILE = os.environ.get("CUST_PROFILE", "rrp-ephemeral-customer")
RC_PROFILE = os.environ.get("RC_PROFILE", "rrp-ephemeral-rc")
ENVS_FILE = os.environ.get("ENVS_FILE", ".ephemeral-envs")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "15"))
POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT", "600"))

ROSACTL = "./bin/rosactl"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def banner(text):
    print(f"\n{'=' * 60}")
    print(f"  {text}")
    print(f"{'=' * 60}")


def die(msg):
    print(f"\nERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def run(cmd, *, env=None, check=True, capture=False):
    """Run a command, streaming output to the terminal unless capture=True."""
    merged_env = {**os.environ, **(env or {})}
    if capture:
        result = subprocess.run(
            cmd,
            env=merged_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if check and result.returncode != 0:
            print(result.stderr, file=sys.stderr)
            die(f"Command failed (exit {result.returncode}): {' '.join(cmd)}")
        return result
    else:
        result = subprocess.run(cmd, env=merged_env)
        if check and result.returncode != 0:
            die(f"Command failed (exit {result.returncode}): {' '.join(cmd)}")
        return result


def cust_env():
    """Return an env dict that activates the customer AWS profile."""
    return {"AWS_PROFILE": CUST_PROFILE}


# ---------------------------------------------------------------------------
# Step 0: Parse args + .ephemeral-envs
# ---------------------------------------------------------------------------


def parse_envs_line(line):
    """Parse a space-separated KEY=VALUE line from .ephemeral-envs into a dict."""
    parts = line.strip().split()
    result = {"_id": parts[0]}
    for part in parts[1:]:
        if "=" in part:
            k, _, v = part.partition("=")
            result[k] = v
    return result


def resolve_config(eph_id):
    if not os.path.isfile(ENVS_FILE):
        die(f"Environments file not found: {ENVS_FILE}")

    with open(ENVS_FILE) as f:
        for line in f:
            if line.startswith(eph_id + " "):
                fields = parse_envs_line(line)
                api_url = fields.get("API_URL")
                region = fields.get("REGION")
                if not api_url:
                    die(f"API_URL not found for ephemeral ID {eph_id} in {ENVS_FILE}")
                if not region:
                    die(f"REGION not found for ephemeral ID {eph_id} in {ENVS_FILE}")
                return api_url, region

    die(f"Ephemeral ID '{eph_id}' not found in {ENVS_FILE}")


def get_account_id():
    result = run(
        ["aws", "sts", "get-caller-identity", "--output", "json"],
        env=cust_env(),
        capture=True,
    )
    identity = json.loads(result.stdout)
    account_id = identity.get("Account")
    if not account_id:
        die("Could not parse Account from sts get-caller-identity output")
    return account_id


# ---------------------------------------------------------------------------
# Step 1: Parallel cluster-iam + cluster-vpc create  [customer account]
# ---------------------------------------------------------------------------


def step_parallel_create(cluster_name, region, az):
    banner("Step 1: cluster-iam create + cluster-vpc create (parallel)")

    env = {**os.environ, **cust_env()}

    iam_cmd = [ROSACTL, "cluster-iam", "create", cluster_name, "--region", region]
    vpc_cmd = [
        ROSACTL,
        "cluster-vpc",
        "create",
        cluster_name,
        "--region",
        region,
        "--availability-zones",
        az,
    ]

    print(f"  + {' '.join(iam_cmd)}")
    print(f"  + {' '.join(vpc_cmd)}")
    print()

    iam_proc = subprocess.Popen(iam_cmd, env=env)
    vpc_proc = subprocess.Popen(vpc_cmd, env=env)

    iam_rc = iam_proc.wait()
    vpc_rc = vpc_proc.wait()

    if iam_rc != 0:
        die(f"cluster-iam create failed (exit {iam_rc})")
    if vpc_rc != 0:
        die(f"cluster-vpc create failed (exit {vpc_rc})")

    print("\nBoth completed successfully.")


# ---------------------------------------------------------------------------
# Step 2: Register customer account with Platform API  [RC account, SigV4 curl]
# ---------------------------------------------------------------------------


def export_rc_credentials():
    """
    Run `aws configure export-credentials --format env` for the RC profile
    and parse the output into a dict of AWS credential env vars.
    """
    result = run(
        [
            "aws",
            "configure",
            "export-credentials",
            "--format",
            "env",
            "--profile",
            RC_PROFILE,
        ],
        capture=True,
    )
    creds = {}
    for line in result.stdout.splitlines():
        # Lines look like: export AWS_ACCESS_KEY_ID=AKIA...
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" in line:
            k, _, v = line.partition("=")
            creds[k.strip()] = v.strip()
    return creds


def step_register_account(api_url, region, account_id):
    banner("Step 2: Register customer account with Platform API (RC account)")

    creds = export_rc_credentials()

    access_key = creds.get("AWS_ACCESS_KEY_ID")
    secret_key = creds.get("AWS_SECRET_ACCESS_KEY")
    session_token = creds.get("AWS_SESSION_TOKEN")

    if not access_key or not secret_key:
        die(
            "Could not extract AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY from RC profile"
        )

    endpoint = f"{api_url}/api/v0/accounts"
    payload = json.dumps({"accountId": account_id, "privileged": True})

    curl_cmd = [
        "curl",
        "-s",
        "--aws-sigv4",
        f"aws:amz:{region}:execute-api",
        "--user",
        f"{access_key}:{secret_key}",
        "-X",
        "POST",
        endpoint,
        "-H",
        "Content-Type: application/json",
        "-d",
        payload,
    ]

    if session_token:
        curl_cmd += ["-H", f"x-amz-security-token: {session_token}"]

    print(f"  POST {endpoint}")
    print(f"  accountId={account_id} privileged=true")
    print()

    result = run(curl_cmd, capture=True)
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    # Treat an HTTP error body as a warning, not a hard failure —
    # the account may already be registered.
    if '"error"' in result.stdout.lower() or '"message"' in result.stdout.lower():
        print("  WARNING: API response may indicate an error; inspect output above.")
    else:
        print("  Account registration succeeded.")


# ---------------------------------------------------------------------------
# Step 3: rosactl login + cluster create  [customer account]
# ---------------------------------------------------------------------------


def step_login_and_create(api_url, cluster_name, region):
    banner("Step 3: rosactl login + cluster create")

    run(
        [ROSACTL, "login", "--url", api_url],
        env=cust_env(),
    )
    run(
        [ROSACTL, "cluster", "create", cluster_name, "--region", region],
        env=cust_env(),
    )


# ---------------------------------------------------------------------------
# Step 4: Poll cluster list for oidc_issuer_url  [customer account]
# ---------------------------------------------------------------------------


def step_poll_issuer(cluster_name, region):
    banner("Step 4: Polling for OIDC issuer URL")

    deadline = time.monotonic() + POLL_TIMEOUT
    attempt = 0

    while True:
        attempt += 1
        elapsed = int(time.monotonic() - (deadline - POLL_TIMEOUT))

        result = run(
            [ROSACTL, "cluster", "list", "--region", region, "-o", "json"],
            env=cust_env(),
            capture=True,
            check=False,
        )

        if result.returncode == 0 and result.stdout.strip():
            try:
                clusters = json.loads(result.stdout)
                items = (
                    clusters
                    if isinstance(clusters, list)
                    else clusters.get("items", [])
                )
                for item in items:
                    if item.get("name") == cluster_name:
                        issuer = item.get("oidc_issuer_url", "")
                        if issuer:
                            print(f"  Found issuer URL: {issuer}")
                            return issuer
            except json.JSONDecodeError:
                pass  # output not yet valid JSON; keep polling

        if time.monotonic() >= deadline:
            die(
                f"Timed out after {POLL_TIMEOUT}s waiting for oidc_issuer_url "
                f"for cluster '{cluster_name}'."
            )

        print(
            f"  [{elapsed}s elapsed] issuer not ready yet, retrying in {POLL_INTERVAL}s..."
        )
        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Step 5: cluster-oidc create  [customer account]
# ---------------------------------------------------------------------------


def step_oidc_create(cluster_name, region, issuer):
    banner("Step 5: cluster-oidc create")

    run(
        [
            ROSACTL,
            "cluster-oidc",
            "create",
            cluster_name,
            "--region",
            region,
            "--oidc-issuer-url",
            issuer,
        ],
        env=cust_env(),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    if len(sys.argv) != 3:
        print(
            "Usage: python3 scripts/dev/setup-cluster.py <ephemeral-id> <cluster-name>\n"
            "\n"
            "  ephemeral-id   8-char ID from .ephemeral-envs (e.g. 2d5e9171)\n"
            "  cluster-name   Name for the new cluster       (e.g. psav-eventbridge)\n"
            "\n"
            "Environment overrides:\n"
            "  CUST_PROFILE   Customer AWS profile  (default: rrp-ephemeral-customer)\n"
            "  RC_PROFILE     RC AWS profile        (default: rrp-ephemeral-rc)\n"
            "  ENVS_FILE      Envs file path        (default: .ephemeral-envs)\n"
            "  POLL_INTERVAL  Poll interval (s)     (default: 15)\n"
            "  POLL_TIMEOUT   Poll timeout (s)      (default: 600)\n",
            file=sys.stderr,
        )
        sys.exit(1)

    eph_id = sys.argv[1]
    cluster_name = sys.argv[2]

    # Resolve config from .ephemeral-envs
    api_url, region = resolve_config(eph_id)
    az = f"{region}a"

    # Derive account ID from customer profile
    account_id = get_account_id()

    banner("Configuration")
    print(f"  Ephemeral ID : {eph_id}")
    print(f"  Cluster name : {cluster_name}")
    print(f"  API URL      : {api_url}")
    print(f"  Region       : {region}")
    print(f"  AZ           : {az}")
    print(f"  Account ID   : {account_id}")
    print(f"  Cust profile : {CUST_PROFILE}")
    print(f"  RC profile   : {RC_PROFILE}")

    step_parallel_create(cluster_name, region, az)
    step_register_account(api_url, region, account_id)
    step_login_and_create(api_url, cluster_name, region)
    issuer = step_poll_issuer(cluster_name, region)
    step_oidc_create(cluster_name, region, issuer)

    banner("Done")
    print(f"  Cluster '{cluster_name}' is ready.")
    print(f"  OIDC issuer : {issuer}")


if __name__ == "__main__":
    main()
