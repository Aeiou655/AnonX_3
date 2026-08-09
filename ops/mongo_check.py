#!/usr/bin/env python3
"""Quick MongoDB connectivity diagnostic for AnonX_3 VPS deployments."""

import os
import sys
import urllib.parse
import socket

from dotenv import load_dotenv

load_dotenv()

MONGO_URL = os.getenv("MONGO_URL", "").strip()

print("=" * 60)
print("AnonX_3 MongoDB Diagnostic")
print("=" * 60)

if not MONGO_URL:
    print("\n[FAIL] MONGO_URL is empty or not set in .env")
    sys.exit(1)

# Strip accidental surrounding quotes (common copy-paste mistake)
MONGO_URL = MONGO_URL.strip('"').strip("'")

# Parse host without leaking credentials
try:
    parsed = urllib.parse.urlparse(MONGO_URL)
    host = parsed.hostname or "UNKNOWN"
    port = parsed.port or (27017 if parsed.scheme == "mongodb" else 27017)
    scheme = parsed.scheme or "UNKNOWN"
    username = parsed.username or "(none)"
    db_path = parsed.path or "(default)"
except Exception as e:
    print(f"\n[FAIL] MONGO_URL is malformed: {e}")
    sys.exit(1)

print(f"\nScheme : {scheme}")
print(f"Host   : {host}")
print(f"Port   : {port}")
print(f"User   : {username}")
print(f"DB Path: {db_path}")

# DNS check
print(f"\n--- DNS Check ---")
try:
    ip = socket.gethostbyname(host)
    print(f"[OK]   {host} resolves to {ip}")
except socket.gaierror as e:
    print(f"[FAIL] Cannot resolve {host}: {e}")
    print("       → Check your VPS DNS or verify the cluster name in MONGO_URL")
    sys.exit(1)

# TCP connectivity check
print(f"\n--- TCP Check ---")
try:
    sock = socket.create_connection((host, port), timeout=10)
    print(f"[OK]   TCP connection to {host}:{port} succeeded")
    sock.close()
except Exception as e:
    print(f"[FAIL] Cannot reach {host}:{port}: {e}")
    print("       → Most likely cause: MongoDB Atlas IP whitelist blocks this VPS.")
    print("       → Fix: https://cloud.mongodb.com → Network Access → Add IP Address")
    sys.exit(1)

# dnspython check (required for mongodb+srv:// Atlas URLs)
print(f"\n--- DNSPython Check ---")
try:
    import dns.resolver
    print("[OK]   dnspython is installed")
except ImportError:
    print("[FAIL] dnspython is NOT installed")
    print("       → This is REQUIRED for MongoDB Atlas (mongodb+srv://) URLs.")
    print("       → Fix: pip3 install dnspython>=2.7.0")
    sys.exit(1)

# MongoDB ping check
print(f"\n--- MongoDB Ping Check ---")
try:
    from pymongo import MongoClient
    client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=10000)
    client.admin.command("ping")
    print("[OK]   MongoDB ping succeeded")
    client.close()
except Exception as e:
    print(f"[FAIL] MongoDB ping failed: {e}")
    print("       → If TCP works but ping fails: wrong password, wrong username,")
    print("         or auth database mismatch in MONGO_URL")
    sys.exit(1)

print("\n" + "=" * 60)
print("All checks passed. MongoDB is reachable from this VPS.")
print("=" * 60)
