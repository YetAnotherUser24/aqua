#!/usr/bin/env python3
"""
aqua-station Cloudflare R2 Synchronization Tool.

Syncs telemetry CSVs and experiment bundles to Cloudflare R2 Object Storage (aqua-station-r2).
Regenerates and uploads the 24/7 client-side SCADA Data Portal (index.html) matching the AquaStation Obsidian theme.
"""
import os
import sys
import json
import gzip
import shutil
import sqlite3
import time
from pathlib import Path
import urllib.request
import boto3
from botocore.config import Config

ENV_FILE = Path("/etc/aqua/gateway.env")
TEMPLATE_PATH = Path("/opt/aqua-station/tools/portal_template.html")
LOCAL_TEMPLATE_PATH = Path(__file__).resolve().parent / "portal_template.html"

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

def fetch_experiments_catalog(s3, bucket):
    """
    Builds the full experiment list.
    Combines direct R2 storage contents with Gateway API metadata if available.
    """
    # 1. Fetch metadata from local gateway or public live gateway if available
    api_titles = {}
    api_roles = {}
    for url in ["http://localhost:8000/api/experiments", "http://127.0.0.1:8000/api/experiments", "https://aqua-station.live/api/experiments"]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Aqua-R2-Sync/1.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
                exp_list = data.get("experiments", [])
                for item in exp_list:
                    eid = item.get("id")
                    if eid:
                        if item.get("title"):
                            api_titles[eid] = item["title"]
                        if item.get("roles"):
                            api_roles[eid] = item["roles"]
            if api_titles:
                break
        except Exception:
            continue

    # 2. Enumerate objects in R2 under exports/
    paginator = s3.get_paginator("list_objects_v2")
    experiments_map = {}

    for page in paginator.paginate(Bucket=bucket, Prefix="exports/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            parts = key.split("/")
            if len(parts) >= 3 and parts[1].startswith("EXP-"):
                dir_name = parts[1]
                fname = parts[2]
                is_native = dir_name.endswith("_native")
                base_id = dir_name[:-7] if is_native else dir_name

                if base_id not in experiments_map:
                    experiments_map[base_id] = {
                        "id": base_id,
                        "title": api_titles.get(base_id, ""),
                        "time_range": {},
                        "roles": api_roles.get(base_id, []),
                        "wide_csv": None,
                        "wide_bundle": None,
                        "native_csv": None,
                        "native_bundle": None,
                        "sort_time": base_id
                    }

                entry = experiments_map[base_id]
                if is_native:
                    if fname.endswith((".tar.gz", ".tgz")):
                        entry["native_bundle"] = key
                    elif fname.endswith(".csv"):
                        entry["native_csv"] = key
                else:
                    if fname.endswith((".tar.gz", ".tgz")):
                        entry["wide_bundle"] = key
                    elif fname.endswith(".csv"):
                        entry["wide_csv"] = key
                    elif fname == "manifest.json" and not entry["title"]:
                        try:
                            m_obj = s3.get_object(Bucket=bucket, Key=key)
                            m_json = json.loads(m_obj["Body"].read().decode("utf-8"))
                            if m_json.get("title"):
                                entry["title"] = m_json["title"]
                            if m_json.get("roles_present") and not entry["roles"]:
                                entry["roles"] = m_json["roles_present"]
                        except Exception:
                            pass

    sorted_experiments = sorted(
        experiments_map.values(),
        key=lambda x: x["id"],
        reverse=True
    )
    return sorted_experiments

def build_portal_html(experiments):
    """Injects experiment catalog into the portal HTML template."""
    template_text = None
    if LOCAL_TEMPLATE_PATH.exists():
        template_text = LOCAL_TEMPLATE_PATH.read_text(encoding="utf-8")
    elif TEMPLATE_PATH.exists():
        template_text = TEMPLATE_PATH.read_text(encoding="utf-8")
    
    if not template_text:
        print("Warning: portal template not found, skipping index.html generation")
        return None

    # Experiments JSON sorted descending
    exp_json = json.dumps(experiments, ensure_ascii=False)
    portal_html = template_text.replace(
        "const EXPERIMENTS = __EXPERIMENTS_JSON__;",
        f"const EXPERIMENTS = {exp_json};"
    )
    return portal_html

def sync_all():
    env = load_env()
    bucket = env.get("AQUA_BACKUP__BUCKET", "aqua-station-r2")
    s3 = get_s3_client(env)

    print(f"Connecting to R2 bucket '{bucket}'...")

    # 1. Sync raw telemetry from local disk if running on gateway host
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

    # 2. Sync experiment exports from local disk if running on gateway host
    exports_root = Path("/data/exports")
    if exports_root.exists():
        for exp_dir in sorted(exports_root.iterdir(), reverse=True):
            if not exp_dir.is_dir():
                continue
            dir_name = exp_dir.name
            for item in sorted(exp_dir.glob("*")):
                if not item.is_file():
                    continue
                key = f"exports/{dir_name}/{item.name}"
                ct = "application/gzip" if item.name.endswith((".gz", ".tgz")) else ("text/csv" if item.name.endswith(".csv") else "application/json")
                try:
                    s3.upload_file(str(item), bucket, key, ExtraArgs={"ContentType": ct})
                except Exception as e:
                    print(f"Error uploading {item.name}: {e}")

    # 3. Build complete catalog (from R2 + Gateway)
    print("Enumerating complete catalog from R2 and Gateway API...")
    experiments = fetch_experiments_catalog(s3, bucket)
    print(f"Total experiments cataloged: {len(experiments)}")

    # 4. Regenerate and upload 24/7 Portal index.html
    portal_html = build_portal_html(experiments)
    if portal_html:
        try:
            print("Uploading updated index.html to R2 root...")
            s3.put_object(
                Bucket=bucket,
                Key="index.html",
                Body=portal_html.encode("utf-8"),
                ContentType="text/html",
                CacheControl="no-cache, must-revalidate"
            )
            print(f"Successfully published data portal (index.html) with {len(experiments)} experiments to https://data.aqua-station.live")
        except Exception as e:
            print(f"Error uploading index.html to R2: {e}")

if __name__ == "__main__":
    sync_all()
