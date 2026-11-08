"""Call Tollgate as a developer application, with JSON, streaming, or parallel requests."""

import argparse
import asyncio
import getpass
import json
import os
import uuid

import httpx


async def run(args, key: str) -> None:
    """Reuse one HTTP client and give each intentional generation a unique retry key."""
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        timeout=httpx.Timeout(240, connect=5),
        trust_env=False,
        follow_redirects=False,
    ) as client:

        async def send(index: int):
            """Print the answer and tracking metadata without printing the API key."""
            payload = {
                "model": args.model,
                "messages": [{"role": "user", "content": args.prompt}],
                "max_completion_tokens": args.max_tokens,
                "stream": args.stream,
            }
            headers = {
                "Authorization": "Bearer " + key,
                "Idempotency-Key": args.operation_key or str(uuid.uuid4()),
                "X-Tollgate-Feature": "python-demo",
            }
            print(
                "Operation key (keep this for an uncertain outcome):",
                headers["Idempotency-Key"],
            )
            try:
                async with client.stream(
                    "POST", "/v1/chat/completions", json=payload, headers=headers
                ) as response:
                    request_id = response.headers.get(
                        "X-Tollgate-Request-ID", response.headers.get("X-Request-ID")
                    )
                    print(
                        f"\nRequest {index}: HTTP {response.status_code}; ID {request_id}"
                    )
                    print(
                        "Reserved USD:",
                        response.headers.get("X-Tollgate-Reservation-USD", "none"),
                    )
                    print(
                        "Warden:",
                        response.headers.get("X-Tollgate-Warden-ID", "standalone"),
                    )
                    print(
                        "Control:",
                        response.headers.get("X-Tollgate-Control-Status", "standalone"),
                    )
                    if not response.is_success or not args.stream:
                        data = json.loads(await response.aread())
                        print(json.dumps(data, indent=2))
                        if response.is_success:
                            print(
                                "Actual cost USD:",
                                response.headers.get("X-Tollgate-Cost-USD"),
                            )
                    else:
                        completed = False
                        async for line in response.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                completed = True
                                print("\nStream complete.")
                                break
                            event = json.loads(data)
                            if event.get("error"):
                                print("\n", event["error"])
                                break
                            for choice in event.get("choices", []):
                                print(
                                    choice.get("delta", {}).get("content", ""),
                                    end="",
                                    flush=True,
                                )
                            if event.get("usage"):
                                print("\nToken usage:", event["usage"])
                        if not completed:
                            print(
                                "\nStream incomplete. Inspect its request ID before starting another generation."
                            )
                    if request_id and response.is_success:
                        status = await client.get(
                            "/v1/requests/" + request_id,
                            headers={"Authorization": "Bearer " + key},
                        )
                        print("Request metadata:", status.json())
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                print(
                    "Connection failed:",
                    type(exc).__name__,
                    "Inspect the operation before trying again; no automatic retry was made.",
                )

        await asyncio.gather(*(send(index + 1) for index in range(args.parallel)))
        balance = await client.get(
            "/v1/balance", headers={"Authorization": "Bearer " + key}
        )
        print("\nBalance snapshot (settlement may still be pending):", balance.json())


def main() -> None:
    """Read a customer key privately instead of exposing it in process arguments."""
    parser = argparse.ArgumentParser(description="Tollgate test client")
    parser.add_argument("--model", default="sim-basic")
    parser.add_argument(
        "--base-url", default=os.getenv("TOLLGATE_BASE_URL", "http://127.0.0.1:8000")
    )
    parser.add_argument(
        "--prompt", default="Explain why a credit reservation is useful."
    )
    parser.add_argument("--stream", action="store_true")
    parser.add_argument(
        "--parallel", type=int, choices=range(1, 21), default=1, metavar="1..20"
    )
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument(
        "--operation-key",
        help="Reuse the same key and identical input only when checking an uncertain attempt.",
    )
    args = parser.parse_args()
    if args.operation_key and args.parallel != 1:
        parser.error("--operation-key requires --parallel 1")
    from urllib.parse import urlsplit

    target = urlsplit(args.base_url)
    if (
        target.username
        or target.password
        or target.query
        or target.fragment
        or target.path not in ("", "/")
        or not target.hostname
        or not (
            target.scheme == "https"
            or target.scheme == "http"
            and target.hostname in ("127.0.0.1", "localhost", "::1")
        )
    ):
        parser.error("Use an HTTPS origin, or an HTTP loopback origin for development.")
    key = os.getenv("TOLLGATE_CUSTOMER_KEY") or getpass.getpass(
        "Customer virtual key: "
    )
    asyncio.run(run(args, key.strip()))


if __name__ == "__main__":
    main()
