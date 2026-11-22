//! AWS provider using an embedded adapter to the official Boto3 SDK.
use anyhow::{Context, Result, bail, ensure};
use magicmtn_sdk::plugin::{Capabilities, ErrorCode, ProviderError};
use magicmtn_sdk::{
    BoxFuture, ExecResult, InferenceBackend, InferenceDeployment, InferenceDeploymentBackend,
    InferenceDeploymentRef, InferenceDeploymentSpec, InferenceRequest, InferenceResponse,
    ProviderSandbox, SandboxBackend, ScpDirection,
};
use serde_json::{Value, json};
use std::{path::Path, process::Stdio, time::Duration};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    process::Command,
};

mod inference;
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
const ADAPTER: &str = concat!(
    include_str!("nitro_eks.py"),
    "\n",
    include_str!("aws_bridge.py")
);

pub fn capabilities() -> Capabilities {
    if cfg!(feature = "bedrock-only") {
        return Capabilities::inference(vec!["chat".into()]);
    }
    use magicmtn_sdk::plugin::{InferenceDeploymentOperation as D, SandboxOperation as S};
    let mut result = Capabilities::inference(vec!["chat".into()]).with_inference_deployments(vec![
        D::Create,
        D::Start,
        D::Status,
        D::Stop,
        D::Delete,
    ]);
    result.sandbox_operations = vec![S::Create, S::Terminate, S::Exec, S::ExecBounded];
    result
}
#[derive(Clone, Debug)]
pub struct AwsProvider {
    python: String,
}
impl AwsProvider {
    pub fn new(python: impl Into<String>) -> Self {
        Self {
            python: python.into(),
        }
    }
    pub fn from_env() -> Self {
        Self::new(std::env::var("MAGICMTN_AWS_PYTHON").unwrap_or_else(|_| "python3".into()))
    }
    async fn call(&self, request: Value) -> Result<Value> {
        let mut child = Command::new(&self.python)
            .args(["-c", ADAPTER])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .spawn()
            .context("set MAGICMTN_AWS_PYTHON to Python with Boto3 installed")?;
        let mut input = child.stdin.take().context("adapter stdin missing")?;
        let output = child.stdout.take().context("adapter stdout missing")?;
        tokio::time::timeout(Duration::from_secs(100), async {
            input.write_all(&serde_json::to_vec(&request)?).await?;
            drop(input);
            let mut bytes = Vec::new();
            output.take(768 * 1024 + 1).read_to_end(&mut bytes).await?;
            ensure!(bytes.len() <= 768 * 1024, "AWS adapter response too large");
            ensure!(
                child.wait().await?.success(),
                "AWS adapter failed; check Python and Boto3 installation"
            );
            let value: Value =
                serde_json::from_slice(&bytes).context("invalid AWS adapter response")?;
            if let Some(error) = value.get("error") {
                let code = match error["code"].as_str() {
                    Some("invalid_request") => ErrorCode::InvalidRequest,
                    Some("unsupported_operation") => ErrorCode::UnsupportedOperation,
                    _ => ErrorCode::UpstreamUnavailable,
                };
                return Err(ProviderError {
                    code,
                    retryable: false,
                    message: error["message"]
                        .as_str()
                        .unwrap_or("AWS operation failed")
                        .into(),
                }
                .into());
            }
            Ok(value)
        })
        .await
        .context("AWS operation timed out; outcome may be unknown, reconcile before retrying")?
    }
    async fn deployment(
        &self,
        operation: &str,
        resource: &InferenceDeploymentRef,
    ) -> Result<InferenceDeployment> {
        Ok(serde_json::from_value(
            self.call(json!({"operation":operation,"resource":resource}))
                .await?,
        )?)
    }
    pub async fn suspend(&self, sandbox: &ProviderSandbox) -> Result<()> {
        self.call(json!({"operation":"suspend","sandbox":sandbox}))
            .await?;
        Ok(())
    }
    pub async fn resume(&self, sandbox: &ProviderSandbox) -> Result<()> {
        self.call(json!({"operation":"resume","sandbox":sandbox}))
            .await?;
        Ok(())
    }
}
impl SandboxBackend for AwsProvider {
    fn create<'a>(&'a self, template: &'a str) -> BoxFuture<'a, ProviderSandbox> {
        Box::pin(async move {
            Ok(serde_json::from_value(
                self.call(json!({"operation":"sandbox_create","template":template}))
                    .await?,
            )?)
        })
    }
    fn create_with_ttl<'a>(
        &'a self,
        template: &'a str,
        ttl_seconds: u32,
    ) -> BoxFuture<'a, ProviderSandbox> {
        Box::pin(async move {
            ensure!(ttl_seconds > 0, "invalid AWS sandbox TTL");
            Ok(serde_json::from_value(self.call(json!({"operation":"sandbox_create","template":template,"ttl_seconds":ttl_seconds})).await?)?)
        })
    }
    fn terminate<'a>(&'a self, sandbox: &'a ProviderSandbox) -> BoxFuture<'a, ()> {
        Box::pin(async move {
            self.call(json!({"operation":"terminate","sandbox":sandbox}))
                .await?;
            Ok(())
        })
    }
    fn exec<'a>(
        &'a self,
        sandbox: &'a ProviderSandbox,
        command: &'a str,
        args: &'a [String],
    ) -> BoxFuture<'a, String> {
        Box::pin(async move {
            let r = self
                .exec_bounded(sandbox, command, args, 30_000, 16_384)
                .await?;
            ensure!(!r.timed_out, "AWS sandbox command timed out");
            ensure!(
                r.exit_code == Some(0),
                "AWS sandbox command exited with {:?}",
                r.exit_code
            );
            Ok(r.stdout)
        })
    }
    fn exec_bounded<'a>(
        &'a self,
        sandbox: &'a ProviderSandbox,
        command: &'a str,
        args: &'a [String],
        timeout_ms: u64,
        max_output_bytes: usize,
    ) -> BoxFuture<'a, ExecResult> {
        Box::pin(async move {
            ensure!(
                (1..=30_000).contains(&timeout_ms) && (1..=16_384).contains(&max_output_bytes),
                "invalid exec limits"
            );
            ensure!(
                !command.is_empty() && command.len() <= 256 && !command.contains('\0'),
                "invalid program"
            );
            ensure!(
                args.len() <= 32 && args.iter().all(|a| a.len() <= 2048 && !a.contains('\0')),
                "invalid arguments"
            );
            Ok(serde_json::from_value(self.call(json!({"operation":"exec","sandbox":sandbox,"command":command,"args":args,"timeout_ms":timeout_ms,"max_output_bytes":max_output_bytes})).await?)?)
        })
    }
    fn ssh<'a>(&'a self, _: &'a ProviderSandbox) -> BoxFuture<'a, ()> {
        Box::pin(async {
            bail!("interactive SSH is unsupported; MicroVMs use authenticated HTTPS")
        })
    }
    fn scp<'a>(
        &'a self,
        _: &'a ProviderSandbox,
        _: &'a Path,
        _: &'a Path,
        _: ScpDirection,
    ) -> BoxFuture<'a, ()> {
        Box::pin(async { bail!("SCP is unsupported") })
    }
}
impl InferenceDeploymentBackend for AwsProvider {
    fn create<'a>(
        &'a self,
        spec: &'a InferenceDeploymentSpec,
    ) -> BoxFuture<'a, InferenceDeployment> {
        Box::pin(async move {
            Ok(serde_json::from_value(
                self.call(json!({"operation":"deployment_create","spec":spec}))
                    .await?,
            )?)
        })
    }
    fn start<'a>(&'a self, r: &'a InferenceDeploymentRef) -> BoxFuture<'a, InferenceDeployment> {
        Box::pin(async move { self.deployment("deployment_start", r).await })
    }
    fn status<'a>(&'a self, r: &'a InferenceDeploymentRef) -> BoxFuture<'a, InferenceDeployment> {
        Box::pin(async move { self.deployment("deployment_status", r).await })
    }
    fn stop<'a>(&'a self, r: &'a InferenceDeploymentRef) -> BoxFuture<'a, InferenceDeployment> {
        Box::pin(async move { self.deployment("deployment_stop", r).await })
    }
    fn delete<'a>(&'a self, r: &'a InferenceDeploymentRef) -> BoxFuture<'a, ()> {
        Box::pin(async move {
            self.call(json!({"operation":"deployment_delete","resource":r}))
                .await?;
            Ok(())
        })
    }
}
impl InferenceBackend for AwsProvider {
    fn infer<'a>(&'a self, request: &'a InferenceRequest) -> BoxFuture<'a, InferenceResponse> {
        Box::pin(async move {
            self.infer_inner(request).await.map_err(|error| {
                if error.downcast_ref::<ProviderError>().is_some() {
                    error
                } else {
                    ProviderError {
                        code: ErrorCode::InvalidRequest,
                        retryable: false,
                        message: "invalid AWS inference request".into(),
                    }
                    .into()
                }
            })
        })
    }
}
fn unsupported(message: &str) -> anyhow::Error {
    ProviderError {
        code: ErrorCode::UnsupportedOperation,
        retryable: false,
        message: message.into(),
    }
    .into()
}
fn upstream(message: &str) -> anyhow::Error {
    ProviderError {
        code: ErrorCode::UpstreamUnavailable,
        retryable: false,
        message: message.into(),
    }
    .into()
}

impl AwsProvider {
    pub fn deployment_ref(&self, id: &str) -> Result<InferenceDeploymentRef> {
        let parts: Vec<_> = id.splitn(4, '/').collect();
        ensure!(
            parts.len() == 4
                && parts[0] == "sagemaker"
                && parts[2] == "models"
                && !parts[1].is_empty()
                && parts[1].len() <= 63
                && parts[1]
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'-')
                && !parts[3].is_empty()
                && parts[3].len() <= 256
                && !parts[3].chars().any(char::is_whitespace),
            "use sagemaker/<endpoint-name>/models/<served-model>"
        );
        Ok(InferenceDeploymentRef {
            provider_id: id.into(),
        })
    }
    pub async fn wait_for(
        &self,
        resource: &InferenceDeploymentRef,
        wanted: magicmtn_sdk::InferenceDeploymentState,
        timeout: Duration,
    ) -> Result<InferenceDeployment> {
        ensure!(
            timeout.as_secs() > 0 && timeout.as_secs() <= 3600,
            "invalid wait timeout"
        );
        tokio::time::timeout(timeout, async {
            loop {
                let deployment = InferenceDeploymentBackend::status(self, resource).await?;
                if deployment.state == wanted {
                    return Ok(deployment);
                }
                ensure!(
                    deployment.state != magicmtn_sdk::InferenceDeploymentState::Failed,
                    "SageMaker deployment failed; inspect AWS resource state"
                );
                tokio::time::sleep(Duration::from_secs(2)).await;
            }
        })
        .await
        .context("SageMaker state wait timed out; no lifecycle mutation was repeated")?
    }
}
