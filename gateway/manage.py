"""Local operations: configure, migrate, inspect, and run Tollgate."""

import argparse
import getpass
import json
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def update_env(changes):
    path = ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for key, value in changes.items():
        indexes = [i for i, line in enumerate(lines) if line.startswith(key + "=")]
        if indexes:
            for index in indexes:
                lines[index] = key + "=" + value
        else:
            lines.append(key + "=" + value)
    temporary = path.with_suffix(".env.tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def configure(mode=None, providers=None, database_path=None):
    """Add missing defaults without overwriting credentials or existing wallet paths."""
    from dotenv import dotenv_values

    old = dotenv_values(ROOT / ".env")
    example = dotenv_values(ROOT / ".env.example")
    missing = {key: value or "" for key, value in example.items() if key not in old}
    for key in ("TOLLGATE_DEVELOPMENT_API_KEY", "TOLLGATE_PROVIDER_API_KEY"):
        if not old.get(key):
            missing[key] = secrets.token_urlsafe(32)
    if mode:
        missing["TOLLGATE_MODE"] = mode
    if providers:
        missing["TOLLGATE_ENABLED_PROVIDERS"] = json.dumps(providers)
    if database_path:
        missing["TOLLGATE_DATABASE_PATH"] = database_path
    update_env(missing)
    print("Configuration saved. Existing balances and credentials were preserved.")
    print("Show the owner key locally: python manage.py admin-key")


def run_service(coroutine):
    """Run a long-lived service; SIGTERM (docker stop) stops it as cleanly as Ctrl+C."""
    import asyncio
    import signal

    async def runner():
        if os.name != "nt":
            asyncio.get_running_loop().add_signal_handler(
                signal.SIGTERM, asyncio.current_task().cancel
            )
        await coroutine

    try:
        asyncio.run(runner())
    except asyncio.CancelledError:
        pass


def onboard(parser, args, settings, ledger):
    """Create or reuse a customer, fund it, issue a restricted key, and print the client pack once."""
    from app.routing.providers import load_models
    from app.services.ledger import BillingError
    from app.services.pricing import usd_to_micro

    if bool(args.name) == bool(args.customer_id):
        parser.error("Use exactly one of --name (new customer) or --customer-id (existing)")
    if not args.models:
        parser.error("List the allowed models with --models")
    if args.expires_days is not None and not 1 <= args.expires_days <= 365:
        parser.error("--expires-days must be between 1 and 365")
    catalog = load_models(settings)
    unknown = [model for model in args.models if model not in catalog]
    if unknown:
        parser.error("Not enabled on this gateway: " + ", ".join(unknown) + ". Enabled: " + ", ".join(catalog))
    try:
        credit = usd_to_micro(args.credit) if args.credit else 0
        cap = usd_to_micro(args.spend_cap) if args.spend_cap else None
    except ValueError as exc:
        parser.error(str(exc))
    try:
        if args.name:
            customer = ledger.create_customer(args.name.strip()[:80])
        else:
            ledger.balance(args.customer_id)
            customer = {"id": args.customer_id, "name": "(existing)"}
        if credit:
            ledger.grant(customer["id"], credit, args.reference or "onboard-" + customer["id"])
        if cap is not None:
            ledger.set_cap(customer["id"], cap)
        key = ledger.create_key(customer["id"], args.label, args.models, args.expires_days)
        wallet = ledger.balance(customer["id"])
    except BillingError as exc:
        raise SystemExit(f"Onboarding failed: {exc.code}: {exc.message}")
    base = (args.base_url or settings.public_origin).rstrip("/")
    lines = [
        "=" * 64,
        "TOLLGATE CLIENT PACK",
        "=" * 64,
        f"Customer ID      : {customer['id']}",
        f"Key ID           : {key['id']}",
        f"API key          : {key['api_key']}",
        f"Base URL         : {base}/v1",
        f"Allowed models   : {', '.join(args.models)}",
        f"Key expires      : {'in ' + str(args.expires_days) + ' days' if args.expires_days else 'never (revoke manually)'}",
        f"Starting balance : {wallet['available_usd']} USD",
        f"Spend cap        : {wallet['spend_cap_usd'] or 'none'}",
        "Specification    : docs/api/openapi.json (send this file)",
        "Integration guide: docs/API_GUIDE.md (send this file)",
        f"Support          : {args.support or '(add your support contact)'} - quote X-Request-ID",
        "",
        "Quick test:",
        f'  curl {base}/v1/models -H "Authorization: Bearer {key["api_key"]}"',
        "=" * 64,
        "The API key is shown ONCE and is not stored. Send it over a private channel.",
    ]
    print("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description="Tollgate administration")
    parser.add_argument(
        "command",
        choices=(
            "setup",
            "admin-key",
            "set-provider",
            "migrate",
            "doctor",
            "reconcile",
            "worker",
            "patrol",
            "serve",
            "rotate-admin-key",
            "backup",
            "relay",
            "watchdog",
            "offline-secret",
            "export-openapi",
            "onboard",
        ),
    )
    parser.add_argument(
        "provider", nargs="?", choices=("openai", "anthropic", "gemini")
    )
    parser.add_argument("--mode", choices=("simulator", "live"))
    parser.add_argument(
        "--providers", nargs="+", choices=("openai", "anthropic", "gemini")
    )
    parser.add_argument("--database-path")
    parser.add_argument("--output")
    parser.add_argument("--role", choices=("standalone", "control", "warden"))
    parser.add_argument("--node-id")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--name", help="onboard: new customer name")
    parser.add_argument("--customer-id", help="onboard: issue a new key for an existing customer")
    parser.add_argument("--models", nargs="+", help="onboard: allowed model IDs")
    parser.add_argument("--credit", help="onboard: starting credit in USD, for example 5.00")
    parser.add_argument("--reference", help="onboard: unique credit reference (default onboard-<customer>)")
    parser.add_argument("--expires-days", type=int, help="onboard: key lifetime in days (1-365)")
    parser.add_argument("--spend-cap", help="onboard: lifetime spend cap in USD")
    parser.add_argument("--label", default="Application key")
    parser.add_argument("--base-url", help="onboard: public API base URL for the client pack")
    parser.add_argument("--support", default="", help="onboard: support contact for the client pack")
    args = parser.parse_args()
    if args.command == "export-openapi":
        from app.api_contract import build_client_openapi

        target = Path(args.output) if args.output else ROOT.parent / "docs" / "api" / "openapi.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(build_client_openapi(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print("Client API specification written to", target.resolve())
        from app.api_contract import render_guide

        guide = ROOT.parent / "docs" / "API_GUIDE.md"
        if guide.exists():
            guide.write_text(render_guide(guide.read_text(encoding="utf-8")), encoding="utf-8")
            print("Generated tables refreshed in", guide.resolve())
        return
    if args.command == "setup":
        configure(args.mode, args.providers, args.database_path)
        return
    if args.command == "set-provider":
        if not args.provider:
            parser.error("Specify openai, anthropic, or gemini")
        value = getpass.getpass("Provider key (hidden input): ").strip()
        if (
            len(value) < 20
            or any(c.isspace() for c in value)
            or any(c in value for c in "#'\"\\")
        ):
            parser.error("Key format is invalid")
        update_env({"TOLLGATE_" + args.provider.upper() + "_API_KEY": value})
        print(
            "Provider credential saved locally. Restart gateway and worker to apply it."
        )
        return
    if args.command == "rotate-admin-key":
        update_env({"TOLLGATE_DEVELOPMENT_API_KEY": secrets.token_urlsafe(32)})
        print(
            "Owner key rotated. Restart all gateway processes to invalidate old sessions."
        )
        return
    if args.command == "offline-secret":
        update_env({"TOLLGATE_ALLOWANCE_SECRET": secrets.token_urlsafe(48)})
        print(
            "Offline secret created in .env. Copy the SAME value to the relay host; "
            "restart wardens, control, worker and the relay."
        )
        return
    if args.role:
        os.environ["TOLLGATE_SERVICE_ROLE"] = args.role
    if args.node_id:
        os.environ["TOLLGATE_NODE_ID"] = args.node_id
    elif args.command == "patrol":
        os.environ["TOLLGATE_NODE_ID"] = "patrol-1"
    from app.config import get_settings
    from app.database import Database
    from app.services.ledger import Ledger

    settings = get_settings()
    if args.command == "admin-key":
        print(settings.development_api_key.get_secret_value())
        return
    if args.command == "relay":
        import uvicorn

        uvicorn.run(
            "app.relay:create_relay_app",
            factory=True,
            host=args.host,
            port=args.port,
            access_log=False,
            timeout_graceful_shutdown=200,
        )
        return
    if args.command == "watchdog":
        from app.services.watchdog import run_watchdog

        run_service(run_watchdog(settings))
        return
    database = Database(
        settings.database_path,
        settings.database_url.get_secret_value() if settings.database_url else None,
    )
    ledger = Ledger(database, settings.customer_concurrency)
    try:
        if args.command == "migrate":
            database.initialize()
            print("Schema version 5 is ready. Existing accounting records were preserved.")
        elif args.command == "backup":
            if database.url:
                parser.error("Use pg_dump for PostgreSQL. See docs/OPERATIONS.md.")
            if not args.output:
                parser.error("Provide a new backup path with --output")
            import sqlite3
            from contextlib import closing

            target = Path(args.output).resolve()
            if target.exists():
                parser.error("Backup target already exists; choose a new filename")
            target.parent.mkdir(parents=True, exist_ok=True)
            with database.connection() as source, closing(sqlite3.connect(target)) as destination:
                source.backup(destination)
            print("SQLite snapshot written to", target)
        elif args.command == "serve":
            import uvicorn

            uvicorn.run(
                "app.main:app",
                host=args.host,
                port=args.port,
                access_log=False,
                limit_concurrency=100,
                timeout_graceful_shutdown=200,
            )
        elif args.command == "worker":
            from app.services.worker import run_worker

            run_service(run_worker(settings))
        elif args.command == "patrol":
            from app.services.patrol import run_patrol

            run_service(run_patrol(settings))
        elif args.command == "onboard":
            onboard(parser, args, settings, ledger)
        elif args.command == "reconcile":
            result = ledger.reconciliation()
            print(json.dumps(result, indent=2))
            if not result["ok"]:
                raise SystemExit(1)
        else:
            from app.routing.providers import load_models, price_is_current

            database.check_version()
            models = load_models(settings)
            print("Mode:", settings.mode)
            print("Database:", "PostgreSQL" if database.url else "SQLite development")
            print("Schema: version 5")
            print("Service role:", settings.service_role)
            print("Control outage policy:", settings.control_failure_policy)
            for spec in models.values():
                print(
                    "Model:",
                    spec.id,
                    "| price review:",
                    (
                        "current"
                        if price_is_current(spec, settings.price_max_age_days)
                        else "REQUIRED"
                    ),
                )
            print("Provider credentials: configured, not network-verified")
            print("Ledger integrity:", "PASS" if ledger.reconciliation()["ok"] else "FAIL")
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    finally:
        database.close()


if __name__ == "__main__":
    main()
