# Audit Environment & Runtime Identity

> **Normative authority:** `SKILL.md` is authoritative for verdict semantics, preflight ordering, retry rules, and completion requirements. This reference elaborates procedures and examples only.

## Purpose

A healthy process is not necessarily the intended application, and a correctly running application can still produce misleading audit results when the auditor, test runner, or another live worker shares mutable infrastructure with it. RFF therefore treats runtime identity and audit-environment isolation as explicit preflight obligations.

## Three-stage preflight

1. **`environment_start`** — establish that the documented runtime can start and is reachable.
2. **`runtime_identity`** — establish what is actually serving the target surface.
3. **`environment_collision`** — establish that audit/test activity will not race with unrelated consumers or reuse mutable state in a way that invalidates observations.

Ordinary feature probes begin only after startup and collision checks `SURVIVED`. Runtime identity must be observed before the collision check; if identity is falsified, later probes may still characterize the deployed runtime, but the report must not imply that source code which is not running was tested.

## Runtime identity evidence

Prefer at least two independent observations when practical. Examples:

- container `CMD` / entrypoint plus `ps` command line,
- PID executable/module path,
- HTTP `Server` header plus application-specific route/OpenAPI/build signature,
- service version/build endpoint plus deployed image digest,
- CLI executable path plus `--version`/runtime metadata,
- desktop process identity plus actual GUI/network behavior.

A generic `200 /healthz`, open port, or “container healthy” state is weak identity evidence by itself. The Phase-1 field audit that motivated this rule had healthy containers while port 8000 was served by `services/placeholder.py` rather than the FastAPI application.

## Environment collision surfaces

Check only what is applicable, but deliberately consider:

- RabbitMQ/AMQP queues and virtual hosts,
- Kafka topics/consumer groups,
- database instances, schemas, table namespaces, or shared seed records,
- MinIO/S3 buckets and prefixes,
- Redis/cache namespaces and stream/queue keys,
- filesystem directories and lock files,
- ports/process names where a second instance could intercept traffic,
- organization/tenant/user identities,
- webhook callback endpoints,
- shared test fixtures or external sandbox accounts.

### Example race

A live email worker and integration test both consume queue `email.normalize`. The worker ACKs the message first, so the test sees `QueueEmpty`. This is **audit/test environment interference**, not proof that the product's normalization feature is broken.

Use `failure_pattern=AUDIT_ENVIRONMENT_INTERFERENCE` with a `BLOCKED` or `INCONCLUSIVE` attempt when appropriate. Isolate the environment without changing target product semantics; if isolation requires product/config repair, seal the audit and handle remediation separately.

## Reproduction metadata

`environment_start`, `runtime_identity`, and `environment_collision` attempts require `repro_command`. Capture concrete commands/actions and evidence so another auditor can reproduce the preflight, for example:

```text
docker compose ps
docker inspect <container> --format '{{json .Config.Cmd}}'
docker exec <container> ps aux
curl -si http://localhost:8000/healthz
rabbitmqctl list_queues name consumers messages
psql ... -c 'select current_database(), current_schema()'
mc ls local/bucket
```

The exact commands depend on the product. Never invent a command just to satisfy the field.
