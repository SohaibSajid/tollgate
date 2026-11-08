import itertools
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

GATEWAY = Path(sys.argv[1])
PG_BIN, PG_DATA = "/usr/lib/postgresql/16/bin", "/tmp/pg/data"
OWNER = "owner_" + "o" * 40
APP_URL = "postgresql://tollgate@127.0.0.1:5433/cluster"
BASE = {
    "TOLLGATE_ENVIRONMENT": "development",
    "TOLLGATE_MODE": "simulator",
    "TOLLGATE_DEVELOPMENT_API_KEY": OWNER,
    "TOLLGATE_PROVIDER_API_KEY": "prov_" + "p" * 40,
    "TOLLGATE_DATABASE_URL": APP_URL,
    "TOLLGATE_WORKER_ENABLED": "false",
    "TOLLGATE_CONTROL_FAILURE_POLICY": "fail_open",
    "TOLLGATE_EXPECTED_WARDENS": '["warden-1","warden-2"]',
    "TOLLGATE_SIMULATOR_DATABASE_PATH": "/tmp/cluster-sim.db",
    "TOLLGATE_HEARTBEAT_SECONDS": "1",
    "TOLLGATE_NODE_STALE_SECONDS": "5",
}
PORTS = {"control": 8300, "warden-1": 8301, "warden-2": 8302}
procs = {}
results = []


def env(**extra):
    e = {k: v for k, v in os.environ.items() if not k.startswith("TOLLGATE_")}
    e.update(BASE)
    e.update(extra)
    return e


def check(label, condition, detail=""):
    results.append(condition)
    print(("PASS " if condition else "FAIL ") + label + (" — " + str(detail) if detail else ""), flush=True)


def start(name, args, **extra):
    procs[name] = subprocess.Popen([sys.executable, *args], cwd=GATEWAY, env=env(**extra), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def node(name):
    role = "control" if name == "control" else "warden"
    start(name, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORTS[name]), "--no-access-log"], TOLLGATE_SERVICE_ROLE=role, TOLLGATE_NODE_ID=name)


def stop(name, sig=signal.SIGTERM):
    p = procs.pop(name)
    p.send_signal(sig)
    try:
        p.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        p.kill()
    return p.returncode


def ready(name):
    try:
        return httpx.get(f"http://127.0.0.1:{PORTS[name]}/ready", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def wait_for(predicate, seconds=40):
    end = time.time() + seconds
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.3)
    return False


class Balancer:
    """Mirror deploy/wardens.cfg: route only to wardens whose /ready is 200, no retries."""

    def __init__(self):
        self.cycle = itertools.cycle(["warden-1", "warden-2"])

    def post(self, path, **kwargs):
        for _ in range(2):
            name = next(self.cycle)
            if ready(name):
                return httpx.post(f"http://127.0.0.1:{PORTS[name]}{path}", timeout=30, **kwargs)
        raise RuntimeError("no ready warden")


def pg(*args):
    return subprocess.run(["su", "postgres", "-c", f"{PG_BIN}/pg_ctl -D {PG_DATA} -o '-p 5433 -k /tmp/pg' -l /tmp/pg/log " + " ".join(args)], capture_output=True, text=True)


def chat(lb, auth, n, tag):
    body = {"model": "sim-basic", "messages": [{"role": "user", "content": f"hello {n}"}], "max_completion_tokens": 32}
    return lb.post("/v1/chat/completions", json=body, headers={**auth, "Idempotency-Key": f"{tag}-{n}"})


try:
    subprocess.run([f"{PG_BIN}/psql", "-h", "127.0.0.1", "-p", "5433", "-U", "tollgate", "-d", "postgres", "-qc", "drop database if exists cluster", "-c", "create database cluster"], check=True)
    if os.path.exists("/tmp/cluster-sim.db"):
        os.remove("/tmp/cluster-sim.db")
    migrated = subprocess.run([sys.executable, "manage.py", "migrate"], cwd=GATEWAY, env=env(TOLLGATE_DATABASE_URL="postgresql://tollgate@127.0.0.1:5433/cluster"), capture_output=True, text=True)
    check("migrate", migrated.returncode == 0, migrated.stderr[-200:])
    start("simulator", ["-m", "uvicorn", "main:app", "--app-dir", str(GATEWAY.parent / "simulator"), "--port", "8001", "--no-access-log"])
    node("control")
    start("worker", ["manage.py", "worker"])
    start("patrol", ["manage.py", "patrol", "--node-id", "patrol-1"])
    node("warden-1")
    node("warden-2")
    check("control, warden-1 and warden-2 all ready", wait_for(lambda: all(ready(n) for n in PORTS)))

    w1 = f"http://127.0.0.1:{PORTS['warden-1']}"
    ctl = f"http://127.0.0.1:{PORTS['control']}"
    owner = {"Authorization": "Bearer " + OWNER}
    r = httpx.get(w1 + "/admin/overview", headers=owner)
    check("admin API is not served by a warden", r.status_code == 404 and r.json()["error"]["code"] == "wrong_service")
    r = httpx.post(ctl + "/v1/chat/completions", json={})
    check("generation is not served by control", r.status_code == 404 and r.json()["error"]["code"] == "wrong_service")

    pack = subprocess.run([sys.executable, "manage.py", "onboard", "--name", "Cluster Co", "--models", "sim-basic", "--credit", "5.00"], cwd=GATEWAY, env=env(), capture_output=True, text=True)
    key = re.search(r"API key\s+: (tg_\S+)", pack.stdout).group(1)
    auth = {"Authorization": "Bearer " + key}
    lb = Balancer()

    served = [chat(lb, auth, n, "a") for n in range(6)]
    wardens = {r.headers.get("X-Tollgate-Warden-ID") for r in served}
    check("traffic spread across both wardens", all(r.status_code == 200 for r in served) and wardens == {"warden-1", "warden-2"}, wardens)

    stop("warden-1")
    check("warden-1 stopped -> its /ready fails", not ready("warden-1"))
    after = [chat(lb, auth, n, "b") for n in range(6)]
    check("all requests still succeed via warden-2", all(r.status_code == 200 and r.headers["X-Tollgate-Warden-ID"] == "warden-2" for r in after), [r.status_code for r in after])

    node("warden-1")
    check("warden-1 restarted immediately after SIGTERM and rejoined", wait_for(lambda: ready("warden-1")))

    drained = httpx.post(ctl + "/admin/wardens/warden-2/drain", json={"draining": True, "reason": "maintenance test"}, headers=owner)
    check("operator drains warden-2 from control", drained.status_code == 200, drained.status_code)
    check("drained warden-2 reports not ready", wait_for(lambda: not ready("warden-2"), 10))
    after = [chat(lb, auth, n, "c") for n in range(4)]
    check("traffic moves to warden-1 during drain", all(r.headers.get("X-Tollgate-Warden-ID") == "warden-1" for r in after))
    httpx.post(ctl + "/admin/wardens/warden-2/drain", json={"draining": False, "reason": "maintenance done"}, headers=owner)
    check("warden-2 back in service after undrain", wait_for(lambda: ready("warden-2"), 10))

    stop("control")
    degraded = chat(lb, auth, 1, "d")
    check("control down + fail_open: generation continues, marked degraded", wait_for(lambda: chat(lb, auth, int(time.time() * 1000), "d").headers.get("X-Tollgate-Control-Status") == "degraded", 15))
    node("control")
    wait_for(lambda: ready("control"))

    stop("worker", signal.SIGKILL)
    pending = [chat(lb, auth, n, "e") for n in range(3)]
    ids = [r.headers["X-Tollgate-Request-ID"] for r in pending]
    states = {httpx.get(w1 + "/v1/requests/" + i, headers=auth).json()["state"] for i in ids}
    check("worker killed: answers still delivered, charges wait in queue", all(r.status_code == 200 for r in pending) and states == {"usage_recorded"}, states)
    start("worker", ["manage.py", "worker"])
    check("restarted worker settles the backlog", wait_for(lambda: {httpx.get(w1 + "/v1/requests/" + i, headers=auth).json()["state"] for i in ids} == {"settled"}))

    stopped = pg("stop", "-m", "fast")
    r = None
    try:
        r = lb.post("/v1/chat/completions", json={"model": "sim-basic", "messages": [{"role": "user", "content": "x"}]}, headers=auth)
    except RuntimeError:
        pass
    code = r.json()["error"]["code"] if r is not None else "no ready warden (LB removed both)"
    check("database down: requests refused safely", r is None or r.status_code == 503, code)
    pg("start")
    check("database back: wardens become ready again", wait_for(lambda: ready("warden-1") and ready("warden-2"), 60))
    ok = chat(lb, auth, 99, "f")
    check("generation works after database restart", ok.status_code == 200, ok.status_code)

    time.sleep(2)
    recon = httpx.get(ctl + "/admin/reconciliation", headers=owner).json()
    check("ledger reconciliation after all failures", recon["ok"], recon["failures"][:2])
    overview = httpx.get(ctl + "/admin/overview", headers=owner).json()
    settled = [r for r in overview["requests"] if r["state"] == "settled"]
    charges = [e for e in overview["ledger"] if e["kind"] == "charge"]
    check("exactly one charge per settled request", len({e["request_id"] for e in charges}) == len(charges) == len(settled), (len(charges), len(settled)))
    cluster = httpx.get(ctl + "/admin/cluster", headers=owner).json()
    patrol = [n for n in cluster["nodes"] if n["node_id"] == "patrol-1"]
    check("Patrol is reporting", bool(patrol) and patrol[0]["status"] == "online", patrol[:1])
finally:
    for name in list(procs):
        code = stop(name)
    print(f"\n{sum(results)}/{len(results)} checks passed")
