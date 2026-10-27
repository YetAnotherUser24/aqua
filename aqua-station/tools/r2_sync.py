#!/usr/bin/env python3
"""
aqua-station Cloudflare R2 Synchronization Tool.

Syncs telemetry CSVs and experiment bundles to Cloudflare R2 Object Storage (aqua-station-r2).
Regenerates the 24/7 client-side SCADA Data Portal (index.html) matching the AquaStation Obsidian theme.
"""
import os
import sys
import json
import gzip
import shutil
from pathlib import Path
import boto3
from botocore.config import Config

ENV_FILE = Path("/etc/aqua/gateway.env")

def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env

def get_s3_client(env):
    endpoint = env.get("AQUA_BACKUP__ENDPOINT_URL") or os.getenv("AQUA_BACKUP__ENDPOINT_URL", "https://2466a556724af7cbd00d3c3197a35de2.r2.cloudflarestorage.com")
    key_id = env.get("AQUA_BACKUP__ACCESS_KEY_ID") or os.getenv("AQUA_BACKUP__ACCESS_KEY_ID", "127d783da93c9f429bb225acf2802de0")
    secret = env.get("AQUA_BACKUP__SECRET_ACCESS_KEY") or os.getenv("AQUA_BACKUP__SECRET_ACCESS_KEY", "90559386ea9c2558bbefd2f24776932bae27aadabc0bb8bdf7763d5cddd2d94a")
    region = env.get("AQUA_BACKUP__REGION", "auto")
    
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        region_name=region,
        config=Config(signature_version="s3v4")
    )

def sync_all():
    env = load_env()
    bucket = env.get("AQUA_BACKUP__BUCKET", "aqua-station-r2")
    s3 = get_s3_client(env)

    print(f"Connecting to R2 bucket '{bucket}'...")

    # 1. Sync raw telemetry
    telemetry_root = Path("/data/telemetry")
    raw_files = []
    if telemetry_root.exists():
        for role_dir in sorted(telemetry_root.iterdir()):
            if not role_dir.is_dir() or role_dir.name in ("unknown", "events"):
                continue
            role = role_dir.name
            for f in sorted(role_dir.glob("*.csv*")):
                key = f"raw/{role}/{f.name}"
                ct = "application/gzip" if f.name.endswith(".gz") else "text/csv"
                try:
                    s3.upload_file(str(f), bucket, key, ExtraArgs={"ContentType": ct})
                    raw_files.append({"role": role, "name": f.name, "key": key, "size": f.stat().st_size})
                except Exception as e:
                    print(f"Error uploading {f.name}: {e}")

    # 2. Sync experiment exports
    exports_root = Path("/data/exports")
    wide_exps = []
    native_exps = []
    
    if exports_root.exists():
        for exp_dir in sorted(exports_root.iterdir(), reverse=True):
            if not exp_dir.is_dir():
                continue
            exp_id = exp_dir.name
            is_native = exp_id.endswith("_native")
            exp_data = {
                "id": exp_id,
                "is_native": is_native,
                "title": "",
                "status": "completed",
                "time_range": {},
                "roles": [],
                "files": [],
                "csv_key": None,
                "bundle_key": None
            }
            for item in sorted(exp_dir.glob("*")):
                if not item.is_file():
                    continue
                key = f"exports/{exp_id}/{item.name}"
                ct = "application/gzip" if item.name.endswith((".gz", ".tgz")) else ("text/csv" if item.name.endswith(".csv") else "application/json")
                try:
                    s3.upload_file(str(item), bucket, key, ExtraArgs={"ContentType": ct})
                    exp_data["files"].append({"name": item.name, "key": key, "size": item.stat().st_size})
                    if item.name == "telemetry.csv":
                        exp_data["csv_key"] = key
                    elif item.name == "telemetry_native.csv" and not exp_data["csv_key"]:
                        exp_data["csv_key"] = key
                    elif item.name.endswith((".tar.gz", ".tgz")):
                        exp_data["bundle_key"] = key
                    elif item.name == "manifest.json":
                        try:
                            m_json = json.loads(item.read_text(encoding="utf-8"))
                            exp_data["title"] = m_json.get("title", "")
                            exp_data["status"] = m_json.get("status", "completed")
                            exp_data["time_range"] = m_json.get("time_range", {})
                            exp_data["roles"] = m_json.get("roles_present", [])
                        except Exception:
                            pass
                except Exception as e:
                    print(f"Error uploading {item.name}: {e}")

            if is_native:
                native_exps.append(exp_data)
            else:
                wide_exps.append(exp_data)

    print(f"Sync complete. Uploaded {len(wide_exps)} wide experiments, {len(native_exps)} native experiments, {len(raw_files)} raw files.")

if __name__ == "__main__":
    sync_all()
