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

def build_portal_html(experiments):
    """Injects experiment catalog into the portal HTML template."""
    template_text = None
    if TEMPLATE_PATH.exists():
        template_text = TEMPLATE_PATH.read_text(encoding="utf-8")
    elif LOCAL_TEMPLATE_PATH.exists():
        template_text = LOCAL_TEMPLATE_PATH.read_text(encoding="utf-8")
    
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
                    # Update manifest.db so retention pruning and health backlog reflect the flush
                    manifest_path = Path("/var/lib/aqua-gateway/manifest.db")
                    if manifest_path.exists():
                        try:
                            with sqlite3.connect(manifest_path) as conn:
                                stem_path = str(f).replace(".gz", "")
                                now_ts = time.time()
                                conn.execute(
                                    """
                                    UPDATE files
                                       SET flushed_at = ?,
                                           archive_key = ?,
                                           verified = 1,
                                           verify_attempts = verify_attempts + 1
                                     WHERE (path = ? OR path = ? OR path = ?)
                                    """,
                                    (now_ts, key, str(f), stem_path, f"{stem_path}.gz"),
                                )
                                conn.commit()
                        except Exception as m_err:
                            print(f"Warning: could not update manifest for {f.name}: {m_err}")
                except Exception as e:
                    print(f"Error uploading {f.name}: {e}")

    # 2. Sync experiment exports
    exports_root = Path("/data/exports")
    experiments_map = {}
    
    if exports_root.exists():
        for exp_dir in sorted(exports_root.iterdir(), reverse=True):
            if not exp_dir.is_dir():
                continue
            dir_name = exp_dir.name
            is_native = dir_name.endswith("_native")
            base_id = dir_name[:-7] if is_native else dir_name

            if base_id not in experiments_map:
                experiments_map[base_id] = {
                    "id": base_id,
                    "title": "",
                    "time_range": {},
                    "roles": [],
                    "wide_csv": None,
                    "wide_bundle": None,
                    "native_csv": None,
                    "native_bundle": None,
                    "sort_time": base_id
                }

            entry = experiments_map[base_id]

            for item in sorted(exp_dir.glob("*")):
                if not item.is_file():
                    continue
                key = f"exports/{dir_name}/{item.name}"
                ct = "application/gzip" if item.name.endswith((".gz", ".tgz")) else ("text/csv" if item.name.endswith(".csv") else "application/json")
                try:
                    s3.upload_file(str(item), bucket, key, ExtraArgs={"ContentType": ct})
                    if is_native:
                        if item.name.endswith((".tar.gz", ".tgz")):
                            entry["native_bundle"] = key
                        elif item.name.endswith(".csv"):
                            entry["native_csv"] = key
                    else:
                        if item.name.endswith((".tar.gz", ".tgz")):
                            entry["wide_bundle"] = key
                        elif item.name.endswith(".csv"):
                            entry["wide_csv"] = key
                        elif item.name == "manifest.json":
                            try:
                                m_json = json.loads(item.read_text(encoding="utf-8"))
                                if m_json.get("title"): entry["title"] = m_json["title"]
                                if m_json.get("time_range"):
                                    entry["time_range"] = m_json["time_range"]
                                    if m_json["time_range"].get("start"):
                                        entry["sort_time"] = m_json["time_range"]["start"]
                                if m_json.get("roles_present"): entry["roles"] = m_json["roles_present"]
                            except Exception:
                                pass
                except Exception as e:
                    print(f"Error uploading {item.name}: {e}")

    # Sort experiments chronologically by experiment ID descending (EXP-YYYY-MM-DD-NN)
    sorted_experiments = sorted(
        experiments_map.values(),
        key=lambda x: x["id"],
        reverse=True
    )

    print(f"Uploaded {len(sorted_experiments)} unique experiments, {len(raw_files)} raw files.")

    # 3. Regenerate and upload 24/7 Portal index.html
    portal_html = build_portal_html(sorted_experiments)
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
            print(f"Successfully published data portal (index.html) with {len(sorted_experiments)} experiments to https://data.aqua-station.live")
        except Exception as e:
            print(f"Error uploading index.html to R2: {e}")

if __name__ == "__main__":
    sync_all()
