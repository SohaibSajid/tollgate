"""Manual end-to-end check against a running gateway. Not part of the automated suite.

Usage (from the project folder):
    python client/live_smoke.py --base http://127.0.0.1:8000 --model gemini-3.5-flash-lite
The customer key is asked for with hidden input, or read from TOLLGATE_SMOKE_KEY.
"""

import argparse
import getpass
import json
import os
import sys
import time
import uuid

import httpx

results = []


def check(label, condition, detail=""):
    results.append(bool(condition))
    print(("PASS " if condition else "FAIL ") + label + (f" — {detail}" if detail != "" else ""))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--model", required=True)
    parser.add_argument("--wait", type=float, default=15, help="seconds to wait for settlement")
    args = parser.parse_args()
    key = os.environ.get("TOLLGATE_SMOKE_KEY") or getpass.getpass("Customer key (hidden): ").strip()
    auth = {"Authorization": "Bearer " + key}
    base = args.base.rstrip("/")
    client = httpx.Client(base_url=base, timeout=200)

    check("health", client.get("/health").status_code == 200)
    ready = client.get("/ready")
    check("ready", ready.status_code == 200, ready.json())
    models = client.get("/v1/models", headers=auth)
    ids = [m["id"] for m in models.json().get("data", [])] if models.status_code == 200 else []
    check("key accepted and model allowed", args.model in ids, ids or models.text[:200])
    before = client.get("/v1/balance", headers=auth).json()

    body = {"model": args.model, "messages": [{"role": "user", "content": "Reply with exactly: Tollgate works."}], "max_completion_tokens": 64}
    answer = client.post("/v1/chat/completions", json=body, headers={**auth, "Idempotency-Key": "smoke-" + uuid.uuid4().hex})
    ok = answer.status_code == 200
    check("JSON answer", ok, answer.json()["choices"][0]["message"]["content"][:80] if ok else answer.text[:300])
    if not ok:
        return finish()
    usage = answer.json()["usage"]
    request_id = answer.headers["X-Tollgate-Request-ID"]
    check("real token counts reported", usage["prompt_tokens"] > 0 and usage["completion_tokens"] > 0, usage)
    check("usage source", True, answer.headers.get("X-Tollgate-Usage-Source"))
    print("     request id:", request_id, "| cost header:", answer.headers.get("X-Tollgate-Cost-USD"))

    chunks, final_usage, error = [], None, None
    with client.stream("POST", "/v1/chat/completions", json={**body, "stream": True}, headers={**auth, "Idempotency-Key": "smoke-" + uuid.uuid4().hex}) as stream:
        for line in stream.iter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            event = json.loads(line[6:])
            if "error" in event:
                error = event["error"]
            elif event["choices"] and event["choices"][0]["delta"].get("content"):
                chunks.append(event["choices"][0]["delta"]["content"])
            elif event.get("usage"):
                final_usage = event["usage"]
    check("streamed answer with final usage", chunks and final_usage and not error, error or "".join(chunks)[:80])

    deadline = time.time() + args.wait
    status = {}
    while time.time() < deadline:
        status = client.get(f"/v1/requests/{request_id}", headers=auth).json()
        if status.get("state") == "settled":
            break
        time.sleep(1)
    check("settled by the worker", status.get("state") == "settled", status.get("state"))
    check("ledger tokens match the answer", (status.get("input_tokens"), status.get("output_tokens")) == (usage["prompt_tokens"], usage["completion_tokens"]), (status.get("input_tokens"), status.get("output_tokens")))
    after = client.get("/v1/balance", headers=auth).json()
    print("     balance before:", before.get("available_usd"), "after:", after.get("available_usd"), "| cost of first request:", status.get("cost_usd"))
    return finish()


def finish():
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
