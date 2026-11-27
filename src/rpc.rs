//! Combined RPC dispatcher kept local because the framework has separate runners.
use anyhow::{Result, bail};
use magicmtn_provider_aws::{AwsProvider, VERSION, capabilities};
use magicmtn_sdk::plugin::*;
use magicmtn_sdk::{
    InferenceBackend, InferenceDeploymentBackend, SandboxBackend, provider_setup::ProviderSetup,
};
use std::io::{BufRead, Read, Write};

pub async fn run(mut backend: AwsProvider) -> Result<()> {
    let capabilities = capabilities();
    let mut input = std::io::stdin().lock();
    let mut output = std::io::stdout().lock();
    let mut protocol = None;
    loop {
        let mut bytes = zeroize::Zeroizing::new(Vec::new());
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
        // The first frame negotiates the legacy direct RPC or broker startup protocol.
        if protocol.is_none() {
            if let Ok(startup) = serde_json::from_slice::<ProviderStartupInput>(&bytes) {
                drop(bytes);
                if startup.protocol_version != CREDENTIAL_PROTOCOL_VERSION
                    || startup.id != 1
                    || startup.provider != "aws"
                    || (startup.verify_account && startup.credential.is_none())
                {
                    bail!("provider startup protocol or identity mismatch");
                }
                let discovery = startup.credential.is_none();
                let verification = startup.verify_account;
                let manifest = ProviderManifest {
                    credential: Some(AwsProvider::credential_spec()),
                    manifest_version: MANIFEST_VERSION,
                    provider_version: VERSION.into(),
                    capabilities: capabilities.clone(),
                };
                let result = match startup.credential {
                    None => Ok(Value::Description(manifest)),
                    Some(credential) => match backend.clone().with_credential(credential) {
                        Ok(prepared) => {
                            backend = prepared;
                            if verification {
                                backend.verify_account().await.map(Value::VerifiedAccount)
                            } else {
                                Ok(Value::Description(manifest))
                            }
                        }
                        Err(error) => Err(error),
                    },
                };
                let failed = result.is_err();
                let outcome = match result {
                    Ok(value) => Outcome::Ok(value),
                    Err(_) => Outcome::Error(ProviderError {
                        code: ErrorCode::Internal,
                        retryable: false,
                        message: "provider credential initialization failed".into(),
                    }),
                };
                write_response(
                    &mut output,
                    Response {
                        id: startup.id,
                        protocol_version: CREDENTIAL_PROTOCOL_VERSION,
                        provider: "aws".into(),
                        outcome,
                    },
                )?;
                if discovery || verification || failed {
                    return Ok(());
                }
                protocol = Some(CREDENTIAL_PROTOCOL_VERSION);
                continue;
            }
            protocol = Some(PROTOCOL_VERSION);
        }
        let request: Request = serde_json::from_slice(&bytes)
            .map_err(|_| anyhow::anyhow!("invalid provider RPC frame"))?;
        if Some(request.protocol_version) != protocol || request.provider != "aws" {
            bail!("provider protocol or identity mismatch");
        }
        let shutdown = matches!(request.operation, Operation::Shutdown);
        let supported = capabilities.supports(&request.operation)
            && (!matches!(request.operation, Operation::ProviderConfigure { .. })
                || protocol == Some(CREDENTIAL_PROTOCOL_VERSION));
        let result = match request.operation {
            Operation::Describe => Ok(Value::Description(ProviderManifest {
                credential: (protocol == Some(CREDENTIAL_PROTOCOL_VERSION))
                    .then(AwsProvider::credential_spec),
                manifest_version: MANIFEST_VERSION,
                provider_version: VERSION.into(),
                capabilities: {
                    let mut capabilities = capabilities.clone();
                    if protocol == Some(PROTOCOL_VERSION) {
                        capabilities.setup = None;
                    }
                    capabilities
                },
            })),
            Operation::Shutdown => Ok(Value::Done),
            _ if !supported => Err(anyhow::anyhow!("unsupported operation")),
            Operation::ProviderConfigure { options } => backend
                .configure(&options)
                .await
                .map(Value::ConfiguredConnection),
            Operation::SandboxPrepareImage { images } => backend
                .prepare_image(&images)
                .await
                .map(Value::PreparedImage),
            Operation::SandboxStatus { sandbox } => SandboxBackend::status(&backend, &sandbox)
                .await
                .map(Value::SandboxStatus),
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
            protocol_version: request.protocol_version,
            provider: "aws".into(),
            outcome,
        };
        write_response(&mut output, response)?;
        if shutdown {
            return Ok(());
        }
    }
}

fn write_response(output: &mut impl Write, response: Response) -> Result<()> {
    let bytes = serde_json::to_vec(&response)?;
    if bytes.len() >= 1024 * 1024 {
        bail!("provider result exceeds message limit");
    }
    output.write_all(&bytes)?;
    output.write_all(b"\n")?;
    output.flush()?;
    Ok(())
}
