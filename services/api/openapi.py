"""OpenAPI specification generator and validation CLI utility.

Supports generating and validating OpenAPI 3.1 specifications per R23.1.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from services.api.main import create_app


def generate_openapi_spec(app: FastAPI | None = None) -> dict[str, Any]:
    """Generate OpenAPI schema dictionary from FastAPI application instance."""
    target_app = app or create_app(lifespan_enabled=False)
    if target_app.openapi_schema:
        return target_app.openapi_schema

    schema = get_openapi(
        title=target_app.title,
        version=target_app.version,
        openapi_version="3.1.0",
        description=target_app.description,
        routes=target_app.routes,
    )
    target_app.openapi_schema = schema
    return schema


def validate_openapi_spec(schema: dict[str, Any]) -> tuple[bool, list[str]]:
    """Validate that OpenAPI schema satisfies project API standards."""
    errors: list[str] = []

    # Check basic structure
    if "openapi" not in schema:
        errors.append("Missing 'openapi' version field.")
    elif not schema["openapi"].startswith("3.1"):
        errors.append(f"Expected OpenAPI 3.1, found: {schema['openapi']}")

    info = schema.get("info", {})
    if not info.get("title"):
        errors.append("Missing API title in info block.")
    if not info.get("version"):
        errors.append("Missing API version in info block.")

    # Check paths
    paths = schema.get("paths", {})
    if not paths:
        errors.append("No paths registered in OpenAPI specification.")

    v1_paths = [p for p in paths if p.startswith("/v1/")]
    if not v1_paths:
        errors.append("No versioned /v1/... endpoints found in specification.")

    return len(errors) == 0, errors


def main() -> None:
    """CLI entrypoint for OpenAPI generation and verification."""
    parser = argparse.ArgumentParser(description="Generate and validate OpenAPI 3.1 specification.")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Target file path to write generated openapi.json.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate OpenAPI schema structure and return exit code.",
    )
    args = parser.parse_args()

    spec = generate_openapi_spec()
    valid, errors = validate_openapi_spec(spec)

    if args.check:
        if not valid:
            print("[ERROR] OpenAPI schema validation failed:", file=sys.stderr)
            for err in errors:
                print(f"  - {err}", file=sys.stderr)
            sys.exit(1)
        path_count = len(spec.get("paths", {}))
        print(f"[OK] OpenAPI schema valid (v{spec.get('openapi')}, {path_count} paths)")

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(spec, indent=2), encoding="utf-8")
        print(f"[INFO] Exported OpenAPI specification to {args.output}")
    elif not args.check:
        print(json.dumps(spec, indent=2))


if __name__ == "__main__":
    main()
