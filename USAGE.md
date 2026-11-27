# AWS provider usage

## Host configuration

Set configuration on the provider host and restart long-running hosts after changing their environment. Keep credentials out of model parameters, operation RPC payloads, deployment specifications, archives, and logs. The MagicMtn broker sends credentials only through the private startup exchange.

| Variable                                         | Purpose                                                                                                                                             |
| ------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `MAGICMTN_AWS_PYTHON`                            | Python interpreter containing Boto3; defaults to `python3`.                                                                                         |
| `AWS_PROFILE`, standard AWS credential variables | Boto3’s host credential chain for direct use and protocol v1. Workload roles are supported.                                                         |
| `AWS_REGION`, `AWS_DEFAULT_REGION`               | Bedrock checks these in that order and defaults to `us-east-2`. Other services use Boto3 region resolution; set `AWS_DEFAULT_REGION` for local use. |
| `MAGICMTN_AWS_BEDROCK_ROLE_ARN`                  | Role assumed for Bedrock; mandatory for `bedrock-only` requests. Also prevents SageMaker inference through this adapter.                            |
| `MAGICMTN_AWS_MICROVM_IMAGES`                    | JSON map from exact OCI image references to prebuilt Lambda MicroVM image identifiers, preferably version-pinned.                                   |
| `MAGICMTN_AWS_MICROVM_EGRESS_CONNECTORS`         | Optional JSON list of outbound network connector ARNs. No outbound networking is requested by default.                                              |
| `MAGICMTN_AWS_MICROVM_EXECUTION_ROLE_ARN`        | Optional IAM permissions explicitly available to code inside the MicroVM.                                                                           |
| `MAGICMTN_AWS_SANDBOX_BACKEND`                   | Backend for `image:` templates: `microvm` (default) or `nitro-eks`.                                                                                 |
| `MAGICMTN_AWS_NITRO_EKS_CLUSTER`                 | EKS cluster name for Nitro Sandboxes.                                                                                                               |
| `MAGICMTN_AWS_NITRO_EKS_NAMESPACE`               | Nitro workload namespace; defaults to `magicmtn-sandboxes`.                                                                                         |
| `MAGICMTN_AWS_KUBECTL`                           | Path to `kubectl` on the provider host; defaults to `kubectl`.                                                                                      |

Discovery does not start Python, read credentials, or call AWS. Credentials remain on the host; they are never returned by the provider or persisted by the adapter. Configured AWS endpoint overrides are ignored.

## Provider credentials

Registry packages declare protocol v2 and require a credential bundle with exactly `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`. Supply those string fields in a private JSON file to `magic --local provider configure aws --secret-file <path>`. The broker verifies the AWS account through signed STS `GetCallerIdentity`, pins its 12-digit account ID, encrypts the local bundle, and passes the admitted version through the provider's private startup frame. New operations use the rotated bundle when the broker publishes it. The provider uses those explicit keys for all Boto3 clients, including the STS role assumption used for Bedrock; it never falls back to ambient credentials in a broker session.

The current SDK credential declaration requires an exact set of non-empty fields. The broker bundle therefore supports access-key pairs, while temporary session credentials, profiles, and workload roles remain supported through direct library/CLI use and protocol v1's host chain. Configure the AWS region and service-specific non-secret settings on the connection. The Python interpreter with Boto3 must be available to the provider host; the broker host runs installed providers in an isolated environment. For a local MagicMtn host, set `MAGICMTN_AWS_PYTHON` to the absolute interpreter path and `MAGICMTN_AWS_KUBECTL` to the absolute `kubectl` path if it is not on the host PATH.

## SDK connection setup

The provider implements the shared `ProviderSetup` contract. Account verification uses signed STS `GetCallerIdentity`. Configuration returns `ConfiguredConnection` with the stable account ID and nonsecret `MAGICMTN_AWS_*` settings. Workload startup constructs the adapter locally; explicit setup and verification perform the read-only service checks. Credentials arrive through the separate protocol v2 startup frame, while legacy v1 discovery and workload dispatch retain their existing interface.

Pass setup options through `magic provider configure aws --secret-file ./aws-credentials.json --option NAME=VALUE`:

| Option                | Purpose                                                                                      |
| --------------------- | -------------------------------------------------------------------------------------------- |
| `region`              | Region for Lambda MicroVM, ECR, EKS, and SageMaker resources; defaults to `us-east-2`.       |
| `bedrock_role_arn`    | IAM role assumed for Bedrock; required by the `bedrock-only` build.                          |
| `sandbox_backend`     | `microvm` or `nitro-eks`; defaults to `microvm`.                                             |
| `sandbox_platform`    | `linux/arm64` for MicroVMs; `linux/amd64` or `linux/arm64` for prepared Nitro images.        |
| `microvm_images`      | JSON map from OCI references to prepared Lambda MicroVM image IDs or ARNs, with versions.    |
| `nitro_images`        | JSON map from OCI references to prepared ECR image digest references.                        |
| `nitro_eks_cluster`   | Existing EKS cluster with the `isolated-sandbox` runtime class.                              |
| `nitro_eks_namespace` | Existing Kubernetes namespace; defaults to `magicmtn-sandboxes`.                             |

Setup pins `MAGICMTN_AWS_ACCOUNT_ID`, saves the resource region as `MAGICMTN_AWS_REGION`, and saves Bedrock's Ohio entry point as `MAGICMTN_AWS_BEDROCK_REGION=us-east-2`. Resource services can therefore use a different region from Bedrock. Existing pinned account, region, cluster, namespace, and image mapping identities are preserved during reconfiguration. Additional image mappings can be added, and the default sandbox backend can be selected explicitly. Use a separate connection to select another resource scope.

For Nitro, setup reads the selected cluster, namespace, and runtime class, then pins its ARN and certificate hash together with the namespace and runtime class UIDs. Apply the [RBAC example](examples/nitro-eks-rbac.json) before configuration; it includes read-only discovery access scoped to the selected namespace and runtime class. Subsequent verification checks the saved identities. Setup uses existing IAM roles, clusters, namespaces, runtime classes, and images.

## Inference

Use the SDK `chat` task for non-streaming chat and function tools. Its input is `{"messages":[...],"tools":[...]}`. Output contains `message`, `usage`, and `finish_reason`. Function calls/results and reasoning content use the SDK schema. Streaming, multiple choices, other tasks, unknown parameters, and routing or input overrides in model parameters are rejected. Leave the separate model `version` unset and put any serving version in the model reference.

Accepted generation parameters are `temperature`, `top_p`, `top_k`, `min_p`, `max_tokens`, `max_completion_tokens`, `stop`, `presence_penalty`, `frequency_penalty`, `repetition_penalty`, `seed`, `response_format`, `tool_choice`, `parallel_tool_calls`, and `reasoning_effort`. Individual models may support a subset. The provider leaves unspecified defaults to the model.

### Bedrock

Send a chat request with your Bedrock inference profile.

```json
{
  "model": {
    "model": "us.openai.gpt-6.1-sol",
    "provider": "aws",
    "parameters": {"max_completion_tokens": 1024}
  },
  "task": "chat",
  "input": {"messages": [{"role": "user", "content": "Hello"}]}
}
```

The manifest advertises [GPT-6.1 Sol](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-1-sol.html) with a 1,000,000-token context budget and 131,072 maximum output tokens. Model selection is separate from account setup. For the full build, SDK deployment creation with this model performs a read-only `GetInferenceProfile`, returns `ready`, and keeps the original profile ID as `inference_model`. Selection references have the form `bedrock/<invocation-account>/us-east-2/profiles/<profile-id>`. Start and status verify the profile; delete releases the host selection while preserving the shared AWS profile. SageMaker resources retain their explicit capacity start/stop lifecycle. The `bedrock-only` build advertises inference and setup directly.

Use a `us.openai.*` inference profile available to your AWS account. The provider sends signed `InvokeModel` requests to `us-east-2` (Ohio) for US cross-region inference. Supply at most one of `max_tokens` and `max_completion_tokens`. Both set the completion token limit.

If configured, the data-plane role is assumed using the admitted broker credential or the direct host credential chain with a 900-second STS session. Each invocation resolves that role session afresh. Grant invocation permissions for the selected profile and its destination models.

### SageMaker

Use `sagemaker/<name>/models/<served-model>` for a managed deployment or `endpoints/<endpoint-name>/models/<served-model>` for an existing endpoint. The served-model component becomes the request body's `model`. Responses retain the container's reported model. Set the provider to `aws` or leave it unset.

The container must accept an OpenAI chat-completions JSON body at `/invocations` and return the matching response shape. Generic predictors and a server exposing only `/v1/chat/completions` need an adapter. Invocation uses signed `SageMakerRuntime.InvokeEndpoint` with JSON content/accept headers.

Real-time hosting supports dedicated instance types, including GPUs. Serverless uses CPU instances with 1–6 GiB memory and optional provisioned concurrency.

## SageMaker lifecycle

Edit the [real-time](examples/realtime.json) or [serverless](examples/serverless.json) specification with the ECR image, execution role, model identifier, and sizing. `model_data_url` is optional when the container already has or loads the model. `environment` is an optional string map for non-secret configuration. Real-time instance settings and serverless memory/concurrency settings cannot be mixed.

```sh
BIN=./target/debug/magicmtn-provider-aws
"$BIN" deployment create --spec examples/realtime.json
# Save the returned resource.provider_id; use that ID for subsequent operations.
ID=sagemaker/magicmtn-chat-realtime/models/org/served-model
"$BIN" deployment start "$ID" --wait
"$BIN" deployment status "$ID"
"$BIN" deployment stop "$ID" --wait
"$BIN" deployment delete "$ID" --wait
```

Create provisions a Model and EndpointConfig under the caller-chosen name and returns `stopped`, without serving capacity. Start creates the Endpoint. Use `--wait` to poll until `ready`. Set the real-time instance count in the deployment specification. **Capacity stays billable until explicitly stopped and teardown completes.**

Stop deletes the Endpoint but retains restartable Model/EndpointConfig resources. Delete requires the Endpoint to have disappeared before removing those artifacts. Management checks ownership tags and expected configuration; external endpoints can be invoked but are not adopted for lifecycle management. IDs are scoped to the configured account and region, which must stay consistent across operations.

Wait defaults to 600 seconds. The `--timeout-seconds` option accepts 1–3600. Observe an existing transition with the wait command.

```sh
"$BIN" deployment wait "$ID" --state stopped --timeout-seconds 900
```

Wait states are `ready`, `stopped`, and `deleted`. Process shutdown and wait failure do not stop or delete remote resources. After a partial create, reconcile the chosen name: status reports a leftover Model without configuration as `failed`. Confirm that the Endpoint is absent before deleting the Model.

## Prepare Canonical images

Read the immutable Canonical manifests for your MagicMtn installation:

```sh
magic --local images list --format json
```

Build your AWS image from the selected Canonical platform and record its repository and SHA-256 digest as the source mapping key. Include Python 3, Pi, `/opt/magicmtn/runtime`, the included AWS command agent at `/opt/magicmtn-agent.py`, and writable `/workspace` and `/computer` directories. Nitro Jobs run the image as UID 1000, so prepare those directories for that user. Keep the AWS agent's HTTPS and stdin execution entry points from [sandbox/agent.py](sandbox/agent.py).

For Lambda MicroVMs, prepare the `linux/arm64` software in a registered MicroVM image with an active successful version. Add its mapping during connection configuration, replacing the placeholders with your source digest and prepared image:

```sh
magic provider configure aws --secret-file ./aws-credentials.json \
  --option region=us-east-2 \
  --option 'microvm_images={"ghcr.io/usemagicmtn/canonical@sha256:YOUR_ARM64_DIGEST":"arn:aws:lambda:us-east-2:123456789012:microvm-image:magicmtn-agent@1.0"}'
magic images prepare canonical --provider aws
```

Preparation reads the image version, checks its account, region, active state, and ARM64 architecture, and returns a version-pinned launch mapping. An unversioned registered image mapping is resolved to its current active version and saved with that version. Existing mappings for other sessions remain available.

For Nitro, build and publish an agent image based on the matching Canonical platform to your account's ECR registry. Map the Canonical source digest to that prepared ECR digest:

```sh
magic provider configure aws --secret-file ./aws-credentials.json \
  --option region=us-east-2 \
  --option sandbox_backend=nitro-eks \
  --option sandbox_platform=linux/amd64 \
  --option nitro_eks_cluster=agent-sandboxes \
  --option 'nitro_images={"ghcr.io/usemagicmtn/canonical@sha256:YOUR_AMD64_DIGEST":"123456789012.dkr.ecr.us-east-2.amazonaws.com/prepared/canonical@sha256:YOUR_PREPARED_IMAGE_DIGEST"}'
magic images prepare canonical --provider aws
```

Nitro preparation verifies the account-scoped ECR digest and saves the mapping for `image:` templates. Record the platform matching your prepared image and EKS runtime. Preparation uses read-only AWS APIs; image building and upload follow the service-specific steps below.

## Sandbox status

`SandboxBackend::status` and `Operation::SandboxStatus` observe the resource through its configured backend. A running MicroVM reports `running`, a suspended MicroVM reports `stopped`, and a terminated or explicitly absent MicroVM reports `missing`. Pending and transitional states return a readiness error. Authentication failures, service failures, and unknown states remain errors.

Nitro status verifies the scoped Job and Kubernetes UID, then checks pod ownership, runtime class, and readiness. A ready owned pod reports `running`; a finished Job reports `stopped`; an absent Job reports `missing`. Scheduling, image-pull, and termination transitions return readiness errors. Status performs read-only requests and keeps the resource lifetime and ownership checks used by execution and termination.

## Lambda MicroVMs

Lambda MicroVMs run prebuilt application snapshots. `sandbox/agent.py` supplies the command API on port 8080. The Dockerfile runs it as an unprivileged user. Commands use literal argv, default to `/workspace`, and share the MicroVM's isolation boundary with the agent. Shell execution requires explicit `/bin/sh -c`. SSH, SCP, local Dockerfile builds, and OCI imports are unsupported.

Edit the [MicroVM image specification](examples/microvm-image.json) with your region, managed base-image ARN, artifact bucket, and image build role. The role must trust `lambda.amazonaws.com` and access the artifact and build logs. Add needed tools to the Dockerfile.

```sh
(cd sandbox && zip ../magicmtn-agent.zip Dockerfile agent.py)
aws s3 cp magicmtn-agent.zip s3://your-artifact-bucket/magicmtn-agent.zip
aws lambda-microvms list-managed-microvm-images
aws lambda-microvms create-microvm-image --cli-input-json file://examples/microvm-image.json
aws lambda-microvms get-microvm-image --image-identifier arn:aws:lambda:REGION:ACCOUNT_ID:microvm-image:magicmtn-agent
```

Wait for an active successful image version. The agent accepts GET and POST readiness/validation hooks. A zip/Dockerfile artifact, a Lambda MicroVM image, and an OCI reference are distinct resources.

MagicMtn sends `image:<OCI reference>|<session UUID>`. Map the exact OCI reference to your MicroVM image.

```sh
export AWS_DEFAULT_REGION=us-east-2
export MAGICMTN_AWS_MICROVM_IMAGES='{"example/agent:1":"arn:aws:lambda:us-east-2:123456789012:microvm-image:magicmtn-agent@1.0"}'
```

Select `aws` and `example/agent:1` in the session configuration. Keep mappings stable for active sessions and pin image versions. Unmapped references fail before launch. Direct callers can use `microvm:<image-id-or-arn>[@version]|<session UUID>`.

The session UUID is the launch idempotency token. Create returns the MicroVM ID and current state immediately, often `PENDING`. Launch enables managed ingress, auto-suspend after 15 idle minutes, and auto-resume on traffic. The requested TTL defaults to eight hours and is capped at that limit. Terminate requests teardown without waiting for completion. Suspend/resume exist as library methods, without SDK RPC operations. Use the creation region for later operations.

Exec obtains the current endpoint from AWS, ignoring caller endpoint metadata, then creates a one-minute token scoped to port 8080. It probes `/health` for up to 15 seconds and submits one `/exec` POST. Tokens are never persisted or returned. Execution is capped at 30 seconds and 16 KiB retained output across stdout and stderr. The agent drains noisy output and kills the process group on timeout.

## Nitro Sandboxes on EKS

Nitro EKS runs each sandbox as an OCI container in a hardware-isolated pod using `runtimeClassName: isolated-sandbox`. Use an EKS Auto Mode cluster, or provision Nitro-capable nodes following the [AWS isolated pod guide](https://docs.aws.amazon.com/eks/latest/userguide/sandboxing-isolated-setup.html). Install a `kubectl` version compatible with the cluster on the provider host and give that host network access to the EKS API endpoint.

Build and push `sandbox/Dockerfile` to your container registry. The image contains Python and `/opt/magicmtn-agent.py`, runs as UID 1000, and owns `/workspace`. Pin the resulting image digest in the session configuration. The provider starts the agent in `--idle` mode and invokes `--stdio` for commands over Kubernetes exec.

Apply the [Nitro EKS RBAC manifest](examples/nitro-eks-rbac.json) with an operator identity. It creates the `magicmtn-sandboxes` namespace and grants the `magicmtn-aws` Kubernetes group access to Jobs, pod discovery, and pod exec in that namespace. Map the provider host's IAM role to this group through an EKS access entry.

```sh
kubectl apply -f examples/nitro-eks-rbac.json
aws eks create-access-entry --cluster-name agent-sandboxes \
  --principal-arn arn:aws:iam::123456789012:role/magicmtn-provider \
  --kubernetes-groups magicmtn-aws --type STANDARD
export AWS_DEFAULT_REGION=us-east-2
export MAGICMTN_AWS_SANDBOX_BACKEND=nitro-eks
export MAGICMTN_AWS_NITRO_EKS_CLUSTER=agent-sandboxes
```

The IAM role needs `eks:DescribeCluster` for the selected cluster. Boto3 uses the admitted broker credential or the direct host credential chain to sign a short-lived, cluster-bound STS identity token. The provider fetches the endpoint and CA from EKS for each operation and sends the exec token through a private pipe to `kubectl`. Secrets stay on the host.

Select provider `aws` and the agent OCI image in MagicMtn. The host backend setting routes `image:<OCI reference>|<session UUID>` to Nitro EKS. Direct callers can select `nitro-eks:<OCI reference>|<session UUID>` explicitly. `microvm:` templates retain their Lambda routing. Existing sandbox IDs retain their backend when the host default changes.

Each session has a deterministic Job name. Repeating create reconciles an existing provider-owned Job with matching configuration. Create returns its scoped ID and `PENDING`. Exec waits up to 45 seconds, including cluster discovery, for an owned Pod with a ready `sandbox` container. It uses a scoped Pod list/watch, falling back to polling if the existing Role lacks watch permission. Reapply `kubectl apply -f examples/nitro-eks-rbac.json` to enable watches. The command has its own timeout after startup, and the provider checks the Job and Pod identities again before sending it. Preserve the returned ID: it includes account, region, cluster, namespace, and Kubernetes UID. Terminate checks that identity and deletes the Job and its pods together.

If startup expires, the provider reports the last observed scheduling constraint or image-pull blocker, or `Nitro sandbox is not ready` if neither was observed. A removed or terminating Job fails immediately; `sandbox lifetime expired` indicates its deadline was exceeded. These readiness errors occur before command submission. A command transport failure after submission is not replayed. Cold node provisioning may still exceed 45 seconds; the Job remains available until its lifetime expires.

Jobs use a one-vCPU, 512-MiB container, `restartPolicy: Never`, and a zero retry budget. The requested lifetime becomes `activeDeadlineSeconds`, capped at eight hours and defaulting to eight hours. Kubernetes removes finished Jobs and their pods after five minutes. Lifetime enforcement continues independently of the provider process.

The pod uses AWS's `sandbox.amazonaws.com/network-isolated: "true"` annotation, which removes the guest network device while retaining exec over vsock. The pod also disables service-account token mounting and drops Linux capabilities. Commands use literal argv with the same 30-second and 16-KiB bounds as Lambda MicroVMs, including process-group cleanup. See the [AWS network isolation guide](https://docs.aws.amazon.com/eks/latest/userguide/sandboxing.html#removing-network-access) for the runtime's device and vsock behavior.

## Permissions and failures

Connection verification needs STS `GetCallerIdentity`. MicroVM preparation needs `lambda:GetMicrovmImage` and `lambda:GetMicrovmImageVersion`; Nitro preparation needs `ecr:DescribeImages` on the prepared repository. Bedrock model selection needs `bedrock:GetInferenceProfile` under the same invocation role as inference.

The host needs `lambda:RunMicrovm`, `lambda:GetMicrovm`, `lambda:TerminateMicrovm`, and `lambda:CreateMicrovmAuthToken`, with `lambda:SuspendMicrovm`/`lambda:ResumeMicrovm` for those library methods. Connector use additionally needs `lambda:PassNetworkConnector`. CLI commands use `lambda-microvms`, while IAM actions use `lambda:`. Image building/upload needs separate S3, build-role, and appropriate `iam:PassRole` permissions.

SageMaker invocation needs `sagemaker:InvokeEndpoint`. Lifecycle needs Create/Describe/DeleteModel, Create/Describe/DeleteEndpointConfig, Create/Describe/DeleteEndpoint, AddTags/ListTags, and `iam:PassRole`. Its execution role needs access to the container image, model artifact, and container resources. Bedrock needs invocation permissions on the selected profiles and destination models. The host also needs permission to assume the configured data-plane role. AWS enforces regional availability, quotas, and account access at operation time.

Mutations, inference, and command execution have automatic retries disabled. Only read-only readiness/state polling repeats. Reconcile actual remote state after a lost response or timeout before retrying. The adapter deadline is 100 seconds. Allow at least 100 seconds for Nitro EKS exec RPCs, independent of the command timeout; the MagicMtn host uses 110 seconds. Inference inputs are bounded at 4 MiB and raw responses at 768 KiB to fit the SDK's 1 MiB RPC frame. Error messages omit upstream bodies, credentials, tokens, and prompts.
