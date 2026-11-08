import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

GATEWAY = Path(sys.argv[1])
OWNER = "owner_" + "o" * 40
ORIGIN = "https://tollgate.test"
BASE = {
    "TOLLGATE_ENVIRONMENT": "production",
    "TOLLGATE_MODE": "live",
    "TOLLGATE_ENABLED_PROVIDERS": '["gemini"]',
    "TOLLGATE_GEMINI_API_KEY": "AIza" + "F" * 35,
    "TOLLGATE_DEVELOPMENT_API_KEY": OWNER,
    "TOLLGATE_PROVIDER_API_KEY": "prov_" + "p" * 40,
    "TOLLGATE_PUBLIC_ORIGIN": ORIGIN,
    "TOLLGATE_WORKER_ENABLED": "false",
    "TOLLGATE_PROVIDER_TIMEOUT_SECONDS": "10",
    "TOLLGATE_RECOVERY_AFTER_SECONDS": "30",
}
APP_URL = "postgresql://tollgate_app:apppass@127.0.0.1:5433/tollgate"
OWNER_URL = "postgresql://tollgate@127.0.0.1:5433/tollgate"
procs = []


def env(**extra):
    e = {k: v for k, v in os.environ.items() if not k.startswith("TOLLGATE_")}
    e.update(BASE)
    e.update(extra)
    return e


def run(args, **extra):
    return subprocess.run([sys.executable, *args], cwd=GATEWAY, env=env(**extra), capture_output=True, text=True)


def start(args, **extra):
    p = subprocess.Popen([sys.executable, *args], cwd=GATEWAY, env=env(**extra), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    procs.append(p)
    return p


def check(label, condition, detail=""):
    print(("PASS " if condition else "FAIL ") + label + (" — " + str(detail) if detail else ""))


try:
    early = run(["manage.py", "doctor"], TOLLGATE_DATABASE_URL=APP_URL)
    check("doctor refuses an unmigrated production database", early.returncode != 0, early.stderr.strip().splitlines()[-1][:120])
    migrated = run(["manage.py", "migrate"], TOLLGATE_DATABASE_URL=OWNER_URL)
    check("migrate as database owner", migrated.returncode == 0, migrated.stdout.strip() or migrated.stderr[-300:])
    doctor = run(["manage.py", "doctor"], TOLLGATE_DATABASE_URL=APP_URL)
    check("doctor as runtime role", doctor.returncode == 0, " | ".join(doctor.stdout.split("\n")[:3]))
    bad = run(["-c", "from app.config import Settings; Settings()"], TOLLGATE_DATABASE_URL=APP_URL, TOLLGATE_PUBLIC_ORIGIN="http://tollgate.test")
    check("production refuses plain-http origin", bad.returncode != 0)

    start(["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8200", "--no-access-log"], TOLLGATE_DATABASE_URL=APP_URL)
    start(["manage.py", "worker"], TOLLGATE_DATABASE_URL=APP_URL)
    url = "http://127.0.0.1:8200"
    for _ in range(80):
        try:
            if httpx.get(url + "/ready", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    check("/ready is 200 with worker running", httpx.get(url + "/ready").status_code == 200)
    health = httpx.get(url + "/health").json()
    check("/health reports live + production build", health["mode"] == "live", health)
    check("/docs disabled in production", httpx.get(url + "/docs").status_code == 404)
    check("/openapi.json disabled in production", httpx.get(url + "/openapi.json").status_code == 404)
    spec = httpx.get(url + "/api-spec.json")
    check("curated client spec published in production", spec.status_code == 200 and spec.json()["servers"][0]["url"] == ORIGIN and "/admin/overview" not in spec.json()["paths"])
    check("unknown Host header rejected", httpx.get(url + "/health", headers={"Host": "evil.example"}).status_code == 400)
    check("HSTS header sent", "strict-transport-security" in httpx.get(url + "/health").headers)

    login_bad = httpx.post(url + "/auth/login", json={"key": OWNER}, headers={"Origin": "https://evil.example"})
    check("dashboard login rejects wrong Origin", login_bad.status_code == 403, login_bad.json()["error"]["code"])
    login = httpx.post(url + "/auth/login", json={"key": OWNER}, headers={"Origin": ORIGIN})
    cookie = login.headers.get("set-cookie", "")
    check("dashboard login sets Secure HttpOnly SameSite=strict cookie", login.status_code == 200 and all(x in cookie for x in ("Secure", "HttpOnly", "SameSite=strict")))

    pack = run(["manage.py", "onboard", "--name", "Prod Demo", "--models", "gemini-3.5-flash-lite", "--credit", "2.00", "--expires-days", "7", "--base-url", ORIGIN], TOLLGATE_DATABASE_URL=APP_URL)
    check("onboard against PostgreSQL as runtime role", pack.returncode == 0, pack.stderr[-200:])
    key = re.search(r"API key\s+: (tg_\S+)", pack.stdout).group(1)
    auth = {"Authorization": "Bearer " + key}
    check("client pack base URL uses public origin", f"Base URL         : {ORIGIN}/v1" in pack.stdout)
    models = httpx.get(url + "/v1/models", headers=auth).json()
    check("customer sees only allowed model", [m["id"] for m in models["data"]] == ["gemini-3.5-flash-lite"])

    chat = httpx.post(url + "/v1/chat/completions", headers={**auth, "Idempotency-Key": "prod-1"}, json={"model": "gemini-3.5-flash-lite", "messages": [{"role": "user", "content": "hi"}]}, timeout=30)
    body = chat.json()
    check("provider unreachable -> documented 5xx error, not a crash", chat.status_code in (502, 503, 504) and "error" in body, (chat.status_code, body.get("error", {}).get("code")))
    wallet = httpx.get(url + "/v1/balance", headers=auth).json()
    check("no money taken when the provider was never reached", wallet["spent_usd"] == "0.000000" and wallet["reserved_usd"] == "0.000000", wallet)

    owner = {"Authorization": "Bearer " + OWNER}
    recon = httpx.get(url + "/admin/reconciliation", headers=owner).json()
    check("ledger reconciliation ok", recon["ok"], recon)
    import psycopg

    with psycopg.connect(APP_URL) as connection:
        try:
            connection.execute("UPDATE ledger SET amount_micro=amount_micro+1")
            check("runtime role cannot rewrite the ledger", False)
        except psycopg.errors.InsufficientPrivilege:
            check("runtime role cannot rewrite the ledger", True)
    with psycopg.connect(APP_URL) as connection:
        stored = connection.execute("SELECT key_hash, prefix FROM api_keys LIMIT 1").fetchone()
        check("API keys stored hashed, not in plaintext", key not in stored and len(stored[0]) == 64)
finally:
    for p in reversed(procs):
        p.send_signal(signal.SIGTERM)
    for p in reversed(procs):
        try:
            out, _ = p.communicate(timeout=20)
            leaked = [line for line in out.splitlines() if "tg_" in line or OWNER in line or "AIza" in line]
            check(f"process {p.args[2] if len(p.args) > 2 else p.args[1]} stopped; no secrets in its log", not leaked, leaked[:2])
        except subprocess.TimeoutExpired:
            p.kill()
            check("process stopped on SIGTERM", False, p.args)
