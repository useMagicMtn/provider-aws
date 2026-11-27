# Contributing

Start with the [README](README.md) to build the provider from source and the [usage guide](USAGE.md) to configure AWS services. Use the public SDK revision in [SDK_REVISION](SDK_REVISION) with the sibling layout in the README. Keep `Cargo.lock` committed and make dependency updates deliberately. The Boto3 constraint records the version tested with the `lambda-microvms` service model. The `crt` extra supports credentials obtained through `aws login`.

## Architecture

The Rust library implements `ProviderSetup`, `SandboxBackend`, `InferenceBackend`, and `InferenceDeploymentBackend`. The framework provides separate runners, so `src/rpc.rs` combines their protocol handling for legacy v1 and credential-aware v2. `src/main.rs` starts persistent JSON RPC when invoked without arguments and exposes deployment commands. `src/setup.rs` declares the setup contract, and embedded `src/setup.py` implements account selection, image preparation, and serverless model bindings. Each AWS operation launches the embedded `src/aws_bridge.py` adapter with a private JSON request and optional broker credential over stdin. The adapter uses official Boto3 service models and signs requests. The library builds with `--no-default-features`.

The `bedrock-only` feature confines RPC capabilities to Bedrock chat and requires an IAM role for Bedrock invocation. Public sandbox and deployment library methods and deployment CLI commands remain available in this build. Apply IAM permissions appropriate to the operations the host should perform.

## Verification

```sh
cargo fmt --check
cargo test --locked
cargo test --locked --features bedrock-only
cargo check --locked --lib --no-default-features
cargo clippy --locked --all-targets --all-features -- -D warnings
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

Rust tests cover persistent RPC, SDK host compatibility, capability boundaries, setup and prepared-image dispatch, and chat/tool normalization. Python tests use Botocore Stubber to check AWS service shapes and exercise the agent locally, including timeouts and bounded output. The setup suite checks account drift, immutable image selection, status transitions, ECR identity, and read-only Bedrock profile bindings. Both suites run locally without AWS credentials.

For changed inference behavior, smoke-test chat, a function-tool round trip, and usage through the compiled provider. For changed lifecycle behavior, verify create/start/readiness/infer/stop/restart/delete in a development account and confirm teardown. For MicroVM changes, verify image build hooks, create, bounded exec, and terminate. Record artifact digests, remote resource IDs, and outcomes without credentials or prompts.

For Nitro EKS changes, build the agent image, create a sandbox, run bounded exec with literal arguments, terminate it, and verify Job/pod deletion. Create a second sandbox with a short TTL and verify deadline enforcement and automatic cleanup. Exercise warm capacity and a cold node-provisioning launch, recording time to readiness and first-command outcome (including whether startup exceeded 15 seconds). Record scoped resource IDs and outcomes with the release digest.

CI checks the pinned SDK on macOS, x86-64 Linux, and ARM64 Linux. It covers both executable feature modes, the library-only build, Python service-shape tests, and package installation/configuration through the standard SDK host. The automated checks allocate no AWS resources.

## Packaging

```sh
./package.sh
cargo test --locked --test rpc maintainer_archive_runs_with_standard_sdk_host -- --ignored
```

Build natively for each target platform. The script creates `dist/aws-<version>-<os>-<arch>.tar.gz` with the executable, the Apache 2.0 license, and a manifest that records protocol v2 and the executable SHA-256. The separate `aws-<version>-assets.tar.gz` contains the license, documentation, Python requirements, sandbox agent, and examples. Install Python dependencies on the provider host. The archive test verifies the checksum, runs discovery and account verification, and dispatches a stubbed operation through the standard SDK credential broker.

Assign a new version to each changed executable you distribute. Preserve prior versioned artifacts and their digests so consumers can keep existing pins.
