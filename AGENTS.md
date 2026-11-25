# Agent development guide

## Setup and checks

Follow the [README](README.md) for local setup, the [usage guide](USAGE.md) for service configuration, and [CONTRIBUTING.md](CONTRIBUTING.md) for verification and packaging.

```sh
cargo build --locked
cargo fmt --check
cargo test --locked
cargo test --locked --features bedrock-only
cargo check --locked --lib --no-default-features
cargo clippy --locked --all-targets --all-features -- -D warnings
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

Use focused tests while iterating. Run `./package.sh` and the ignored archive compatibility test after packaging changes. Tests use stubs, subprocess fixtures, and local execution. They allocate no AWS resources. Report live validation separately.

## Code map

- `src/lib.rs`: provider construction, capabilities, embedded adapter transport, sandbox/deployment traits, lifecycle references, and bounded polling.
- `src/inference.rs`: Bedrock/SageMaker routing, chat/tool normalization, validation.
- `src/aws_bridge.py`: Boto3 calls, STS role assumption, ownership checks, SageMaker lifecycle, MicroVM endpoint validation, and authenticated agent calls.
- `src/nitro_eks.py`: embedded EKS Nitro Job lifecycle, cluster-bound STS auth, ownership and UID checks, and bounded Kubernetes exec with credentials in pipes.
- `src/rpc.rs`: combined persistent protocol v1 and credential-aware v2 dispatcher.
- `src/main.rs`: RPC entry point and explicit deployment CLI.
- `sandbox/agent.py`, `sandbox/Dockerfile`: bounded command execution and image.
- `tests/rpc.rs`, `tests/fixtures/fake-python.py`: Rust/adapter and SDK host contracts.
- `tests/test_bridge.py`: Boto3 service shapes, credential boundaries, local agent.
- `examples/*.json`: image build and SageMaker deployment specifications.
- `package.sh`: native executable/checksum archive and supporting assets.

Keep changes focused and dependencies stable. Preserve the library build without default features and both executable feature modes.

## Compatibility boundaries

- Preserve provider name `aws`, legacy protocol v1 compatibility and credential-aware v2, public SDK interfaces, CLI, environment variables, resource IDs, and persisted sandbox metadata.
- Keep discovery independent of Python, credentials, and network access. Write RPC or CLI JSON to stdout and diagnostics to stderr.
- Preserve `bedrock-only` chat capabilities, explicit data-plane role requirement, Ohio endpoint, and rejection of SageMaker inference. The library methods and deployment CLI remain available.
- Retain the default build's sandbox operations and five deployment operations. Use the combined dispatcher for all provider traits.
- Keep secrets on the host. Never expose upstream bodies, prompts, credentials, tokens, or raw transport errors. Preserve signed AWS calls, endpoint validation, disabled redirects/endpoint overrides, output bounds, and deadlines.
- Never automatically replay mutations, inference, or exec after uncertain failure. Only read-only polling repeats. Shutdown must leave remote resources alone.
- Preserve SageMaker ownership/account-region scope, two-phase teardown, stopped creation, and reconciliation after partial creation. Never adopt external resources.
- Preserve MicroVM image mappings, session idempotency tokens, TTL cap, creation region checks, freshly fetched endpoints, and short-lived port-scoped auth.
- Keep literal argv, command limits, process-group cleanup, function-call IDs, reasoning content, and token usage. Unsupported operations must fail explicitly.
- Do not commit `.cargo/config.toml`, `setup.env.sh`, `.local/`, credentials, virtual environments, build output, or Python caches. Do not declare a license or publish artifacts without a user request.
