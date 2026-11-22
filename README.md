<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/logo-dark.svg">
    <img src=".github/assets/logo-light.svg" alt="MagicMtn logo" width="180" height="180">
  </picture>
</p>

# AWS provider for MagicMtn

Use AWS with MagicMtn for Bedrock chat and function tools, SageMaker chat and endpoint management, and isolated sandboxes on Lambda MicroVMs or Nitro-enabled EKS.

## Build from source

Use a current stable Rust toolchain with edition 2024 support, Python 3.11 or later, and a sibling `magicmtn` checkout containing the Rust SDK. Run these commands from the AWS provider checkout.

```sh
cp -n .cargo/config.toml.example .cargo/config.toml
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export MAGICMTN_AWS_PYTHON="$PWD/.venv/bin/python"
cargo build --locked
```

For a different checkout layout, update the SDK paths in `.cargo/config.toml`. Use `target/debug/magicmtn-provider-aws` as the executable in a MagicMtn SDK host. The executable accepts newline-delimited MagicMtn provider protocol v1 messages on stdin. To verify discovery, run the following command.

```sh
printf '%s\n' '{"id":0,"protocol_version":1,"provider":"aws","operation":{"name":"describe"}}' \
  | ./target/debug/magicmtn-provider-aws
```

## Connect to AWS

Set AWS credentials on the host that runs the provider. You can select a configured profile with `AWS_PROFILE` or use the host's IAM workload role. Keep `MAGICMTN_AWS_PYTHON` set to the interpreter from the build steps. See [host configuration](USAGE.md#host-configuration) for region, role, and sandbox settings.

| Service                    | How to use it                                                                                                                                                                                                 |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Amazon Bedrock             | Set provider `aws`, task `chat`, and a `us.openai.*` inference profile. See the [Bedrock request example](USAGE.md#bedrock) and [IAM permissions](USAGE.md#permissions-and-failures).                         |
| Amazon SageMaker           | Invoke a chat endpoint or manage real-time and CPU serverless deployments with the `deployment` CLI. See [endpoint setup](USAGE.md#sagemaker) and the [deployment walkthrough](USAGE.md#sagemaker-lifecycle). |
| AWS Lambda MicroVMs        | Build an image with the included command agent and map the OCI reference to the MicroVM image. Follow the [MicroVM setup](USAGE.md#lambda-microvms).                                                          |
| AWS Nitro Sandboxes on EKS | Select the `nitro-eks` backend and an EKS cluster with the isolated sandbox runtime class. Follow the [Nitro EKS setup](USAGE.md#nitro-sandboxes-on-eks).                                                     |

For a host dedicated to Bedrock chat over RPC, build with `cargo build --locked --features bedrock-only` and set `MAGICMTN_AWS_BEDROCK_ROLE_ARN` to the role the provider should assume.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development, testing, and packaging.

## License

[Apache License 2.0](LICENSE).
