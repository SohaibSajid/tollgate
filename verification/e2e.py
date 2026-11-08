import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

GATEWAY = Path(sys.argv[1])
BASE = "http://127.0.0.1:8000"
procs = []


def start(args, cwd):
    p = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    procs.append(p)
    return p


def wait(url, ok=(200,), tries=60):
    for _ in range(tries):
        try:
            if httpx.get(url, timeout=1).status_code in ok:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise SystemExit("not up: " + url)


try:
    start([sys.executable, "-m", "uvicorn", "main:app", "--app-dir", str(GATEWAY.parent / "simulator"), "--host", "127.0.0.1", "--port", "8001", "--no-access-log"], GATEWAY)
    wait("http://127.0.0.1:8001/health")
    start([sys.executable, "manage.py", "serve"], GATEWAY)
    wait(BASE + "/health")
    start([sys.executable, "manage.py", "worker"], GATEWAY)
    wait(BASE + "/ready")
    print("ready:", httpx.get(BASE + "/ready").json())

    pack = subprocess.run(
        [sys.executable, "manage.py", "onboard", "--name", "Acme Demo", "--models", "sim-basic", "--credit", "1.00", "--expires-days", "14", "--spend-cap", "0.50", "--support", "support@example.com"],
        cwd=GATEWAY, capture_output=True, text=True, check=True,
    ).stdout
    key = re.search(r"API key\s+: (tg_\S+)", pack).group(1)
    print(pack.replace(key, key[:10] + "…redacted"))
    auth = {"Authorization": "Bearer " + key}

    print("models:", [m["id"] for m in httpx.get(BASE + "/v1/models", headers=auth).json()["data"]])

    from openai import OpenAI

    client = OpenAI(api_key=key, base_url=BASE + "/v1")
    raw = client.chat.completions.with_raw_response.create(
        model="sim-basic", messages=[{"role": "user", "content": "Say hi"}], max_completion_tokens=64,
        extra_headers={"Idempotency-Key": "sdk-json-1", "X-Tollgate-Feature": "demo"},
    )
    answer = raw.parse()
    request_id = raw.headers["X-Tollgate-Request-ID"]
    print("SDK json:", answer.choices[0].message.content[:50], "| usage", answer.usage.prompt_tokens, answer.usage.completion_tokens, "| cost", raw.headers["X-Tollgate-Cost-USD"])

    parts, usage = [], None
    for chunk in client.chat.completions.create(model="sim-basic", messages=[{"role": "user", "content": "Stream hi"}], max_completion_tokens=64, stream=True):
        if chunk.choices and chunk.choices[0].delta.content:
            parts.append(chunk.choices[0].delta.content)
        if chunk.usage:
            usage = chunk.usage
    print("SDK stream:", "".join(parts)[:50], "| final usage", usage.total_tokens)

    from openai import APIStatusError

    try:
        client.chat.completions.create(model="sim-basic", messages=[{"role": "user", "content": "Say hi"}], max_completion_tokens=64, extra_headers={"Idempotency-Key": "sdk-json-1", "X-Tollgate-Feature": "demo"})
    except APIStatusError as exc:
        print("SDK retry same key ->", exc.status_code, exc.body["code"] if "code" in exc.body else exc.body)

    try:
        client.chat.completions.create(model="sim-deep", messages=[{"role": "user", "content": "x"}])
    except APIStatusError as exc:
        print("SDK disallowed model ->", exc.status_code, exc.body.get("code"))

    time.sleep(2)
    status = httpx.get(BASE + "/v1/requests/" + request_id, headers=auth).json()
    print("request after worker:", status["state"], status["cost_usd"])
    print("balance:", httpx.get(BASE + "/v1/balance", headers=auth).json())
    owner = subprocess.run([sys.executable, "manage.py", "admin-key"], cwd=GATEWAY, capture_output=True, text=True).stdout.strip()
    print("reconcile:", json.loads(subprocess.run([sys.executable, "manage.py", "reconcile"], cwd=GATEWAY, capture_output=True, text=True).stdout)["ok"])
    print("admin with customer key ->", httpx.get(BASE + "/admin/overview", headers=auth).status_code)
    print("admin with owner key ->", httpx.get(BASE + "/admin/overview", headers={"Authorization": "Bearer " + owner}).status_code)
finally:
    for p in reversed(procs):
        p.send_signal(signal.SIGTERM)
    for p in reversed(procs):
        try:
            out, _ = p.communicate(timeout=15)
            print("exit", p.args[2] if len(p.args) > 2 else p.args, p.returncode)
        except subprocess.TimeoutExpired:
            p.kill()
            print("KILLED (did not stop)", p.args)
