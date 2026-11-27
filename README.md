<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/logo-dark.svg">
    <img src=".github/assets/logo-light.svg" alt="MagicMtn logo" width="180" height="180">
  </picture>
</p>

# AWS provider for MagicMtn

Use AWS with MagicMtn for Bedrock chat and function tools, SageMaker chat and endpoint management, and isolated sandboxes on Lambda MicroVMs or Nitro-enabled EKS.

## Build from source

Use a current stable Rust toolchain with edition 2024 support, Python 3.11 or later, and a sibling `magicmtn` checkout at the public SDK revision in [SDK_REVISION](SDK_REVISION). Run these commands from the AWS provider checkout.

```sh
test -f .cargo/config.toml || cp .cargo/config.toml.example .cargo/config.toml
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export MAGICMTN_AWS_PYTHON="$PWD/.venv/bin/python"
cargo build --locked
```

For a different checkout layout, update the SDK paths in `.cargo/config.toml`. Use `target/debug/magicmtn-provider-aws` as the executable in a MagicMtn SDK host. Registry packages use the MagicMtn credential startup protocol v2. The executable also accepts legacy direct protocol v1 requests. To verify credential-independent discovery, run the following command.

```sh
printf '%s\n' '{"id":1,"protocol_version":2,"provider":"aws","credential":null,"verify_account":false}' \
  | ./target/debug/magicmtn-provider-aws
```

## Connect to AWS

Install the provider with `magic provider install aws` and start your standalone MagicMtn server. Save a private `aws-credentials.json` file containing your access-key pair:

```json
{"AWS_ACCESS_KEY_ID":"YOUR_ACCESS_KEY_ID","AWS_SECRET_ACCESS_KEY":"YOUR_SECRET_ACCESS_KEY"}
```

```sh
chmod 600 aws-credentials.json
magic provider configure aws --secret-file ./aws-credentials.json --option region=us-east-2
```

Setup verifies your account through STS and saves nonsecret connection settings separately from the key. Configure a Bedrock role with `--option bedrock_role_arn=ROLE_ARN`. Selectable models include GPT-6.1 Sol; choosing it verifies an existing Bedrock profile. Setup and Bedrock selection perform read-only service checks. See the [setup options](USAGE.md#sdk-connection-setup) and [model selection](USAGE.md#bedrock).

Direct library use, the deployment CLI, and legacy protocol v1 use Boto3's host credential chain, including `AWS_PROFILE` and IAM workload roles. Set `MAGICMTN_AWS_PYTHON` to the interpreter from the build steps on your provider host. See [host configuration](USAGE.md#host-configuration) for runtime, role, and sandbox settings.

| Service                    | How to use it                                                                                                                                                                                                 |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Amazon Bedrock             | Set provider `aws`, task `chat`, and a `us.openai.*` inference profile. See the [Bedrock request example](USAGE.md#bedrock) and [IAM permissions](USAGE.md#permissions-and-failures).                         |
| Amazon SageMaker           | Invoke a chat endpoint or manage real-time and CPU serverless deployments with the `deployment` CLI. See [endpoint setup](USAGE.md#sagemaker) and the [deployment walkthrough](USAGE.md#sagemaker-lifecycle). |
| AWS Lambda MicroVMs        | Build an image with the included command agent and map the OCI reference to the MicroVM image. Follow the [MicroVM setup](USAGE.md#lambda-microvms).                                                          |
| AWS Nitro Sandboxes on EKS | Select the `nitro-eks` backend and an EKS cluster with the isolated sandbox runtime class. Follow the [Nitro EKS setup](USAGE.md#nitro-sandboxes-on-eks).                                                     |

Prepare Canonical for your configured Lambda MicroVM or Nitro backend with `magic images prepare canonical --provider aws`. Preparation validates and pins an existing AWS image built from that Canonical digest. Follow the [prepared-image setup](USAGE.md#prepare-canonical-images) to register the image mapping. Sandbox status observes the current MicroVM state or the owned Nitro Job and pod.

For a host dedicated to Bedrock chat over RPC, build with `cargo build --locked --features bedrock-only` and set `MAGICMTN_AWS_BEDROCK_ROLE_ARN` to the role the provider should assume.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development, testing, and packaging.

## License

[Apache License 2.0](LICENSE).
