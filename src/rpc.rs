//! Combined RPC dispatcher kept local because the framework has separate runners.
use anyhow::{Result, bail};
use magicmtn_provider_aws::{AwsProvider, VERSION, capabilities};
use magicmtn_sdk::plugin::*;
use magicmtn_sdk::{InferenceBackend, InferenceDeploymentBackend, SandboxBackend};
use std::io::{BufRead, Read, Write};

pub async fn run(backend: AwsProvider) -> Result<()> {
    let capabilities = capabilities();
    let mut input = std::io::stdin().lock();
    let mut output = std::io::stdout().lock();
    loop {
        let mut bytes = Vec::new();
        if input
            .by_ref()
            .take(1024 * 1024 + 1)
            .read_until(b'\n', &mut bytes)?
            == 0
        {
            return Ok(());
        }
        if bytes.len() > 1024 * 1024 || !bytes.ends_with(b"\n") {
            bail!("invalid or oversized RPC frame");
        }
        let request: Request = serde_json::from_slice(&bytes)?;
        if request.protocol_version != PROTOCOL_VERSION || request.provider != "aws" {
            bail!("provider protocol or identity mismatch");
        }
        let shutdown = matches!(request.operation, Operation::Shutdown);
        let supported = capabilities.supports(&request.operation);
        let result = match request.operation {
            Operation::Describe => Ok(Value::Description(ProviderManifest {
                manifest_version: MANIFEST_VERSION,
                provider_version: VERSION.into(),
                capabilities: capabilities.clone(),
            })),
            Operation::Shutdown => Ok(Value::Done),
            _ if !supported => Err(anyhow::anyhow!("unsupported operation")),
            Operation::SandboxCreate {
                template,
                ttl_seconds,
            } => match ttl_seconds {
                Some(seconds) => {
                    SandboxBackend::create_with_ttl(&backend, &template, seconds).await
                }
                None => SandboxBackend::create(&backend, &template).await,
            }
            .map(Value::Sandbox),
            Operation::SandboxTerminate { sandbox } => {
                backend.terminate(&sandbox).await.map(|()| Value::Done)
            }
            Operation::SandboxExec {
                sandbox,
                command,
                args,
            } => backend
                .exec(&sandbox, &command, &args)
                .await
                .map(Value::Text),
            Operation::SandboxExecBounded {
                sandbox,
                command,
                args,
                timeout_ms,
                max_output_bytes,
            } => backend
                .exec_bounded(&sandbox, &command, &args, timeout_ms, max_output_bytes)
                .await
                .map(Value::Exec),
            Operation::Infer { request } => backend.infer(&request).await.map(Value::Inference),
            Operation::InferenceDeploymentCreate { spec } => {
                InferenceDeploymentBackend::create(&backend, &spec)
                    .await
                    .map(Value::InferenceDeployment)
            }
            Operation::InferenceDeploymentStart { resource } => backend
                .start(&resource)
                .await
                .map(Value::InferenceDeployment),
            Operation::InferenceDeploymentStatus { resource } => {
                InferenceDeploymentBackend::status(&backend, &resource)
                    .await
                    .map(Value::InferenceDeployment)
            }
            Operation::InferenceDeploymentStop { resource } => backend
                .stop(&resource)
                .await
                .map(Value::InferenceDeployment),
            Operation::InferenceDeploymentDelete { resource } => {
                backend.delete(&resource).await.map(|()| Value::Done)
            }
            _ => Err(anyhow::anyhow!("unsupported operation")),
        };
        let outcome = match result {
            Ok(v) => Outcome::Ok(v),
            Err(e) => Outcome::Error(
                e.downcast_ref::<ProviderError>()
                    .cloned()
                    .unwrap_or(ProviderError {
                        code: if supported {
                            ErrorCode::Internal
                        } else {
                            ErrorCode::UnsupportedOperation
                        },
                        retryable: false,
                        message: if supported {
                            "AWS provider operation failed"
                        } else {
                            "operation is not supported"
                        }
                        .into(),
                    }),
            ),
        };
        let response = Response {
            id: request.id,
            protocol_version: PROTOCOL_VERSION,
            provider: "aws".into(),
            outcome,
        };
        let bytes = serde_json::to_vec(&response)?;
        if bytes.len() >= 1024 * 1024 {
            bail!("provider result exceeds message limit");
        }
        output.write_all(&bytes)?;
        output.write_all(b"\n")?;
        output.flush()?;
        if shutdown {
            return Ok(());
        }
    }
}
