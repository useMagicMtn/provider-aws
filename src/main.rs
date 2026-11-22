use anyhow::{Result, ensure};
use clap::{Parser, Subcommand, ValueEnum};
use magicmtn_provider_aws::AwsProvider;
mod rpc;
use magicmtn_sdk::{InferenceDeploymentBackend, InferenceDeploymentSpec, InferenceDeploymentState};
use serde_json::json;
use std::time::Duration;

#[derive(Parser)]
#[command(
    version,
    about = "AWS inference provider and deployment controls for MagicMtn"
)]
struct Cli {
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand)]
enum Command {
    /// Manage serving resources explicitly; credentials come from the environment
    Deployment {
        #[command(subcommand)]
        command: DeploymentCommand,
    },
}

#[derive(Subcommand)]
enum DeploymentCommand {
    /// Provision from a JSON specification; save the returned resource before waiting
    Create {
        #[arg(long)]
        spec: std::path::PathBuf,
    },
    /// Create the configured serving endpoint; poll until ready
    Start {
        id: String,
        #[arg(long)]
        wait: bool,
        #[arg(long, default_value_t = 600)]
        timeout_seconds: u64,
    },
    /// Inspect status without starting capacity
    Status { id: String },
    /// Disable serving capacity; use --wait to confirm teardown
    Stop {
        id: String,
        #[arg(long)]
        wait: bool,
        #[arg(long, default_value_t = 600)]
        timeout_seconds: u64,
    },
    /// Delete model/configuration artifacts after stop has completed
    Delete {
        id: String,
        #[arg(long)]
        wait: bool,
        #[arg(long, default_value_t = 600)]
        timeout_seconds: u64,
    },
    /// Wait for readiness or teardown without repeating a lifecycle mutation
    Wait {
        id: String,
        #[arg(long, value_enum, default_value = "ready")]
        state: WaitState,
        #[arg(long, default_value_t = 600)]
        timeout_seconds: u64,
    },
}

#[derive(Clone, Copy, ValueEnum)]
enum WaitState {
    Ready,
    Stopped,
    Deleted,
}
impl From<WaitState> for InferenceDeploymentState {
    fn from(state: WaitState) -> Self {
        match state {
            WaitState::Ready => Self::Ready,
            WaitState::Stopped => Self::Stopped,
            WaitState::Deleted => Self::Deleted,
        }
    }
}

fn wait_timeout(seconds: u64) -> Result<Duration> {
    ensure!(
        (1..=3600).contains(&seconds),
        "timeout-seconds must be between 1 and 3600"
    );
    Ok(Duration::from_secs(seconds))
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    let provider = AwsProvider::from_env();
    let Some(Command::Deployment { command }) = cli.command else {
        return rpc::run(provider).await;
    };
    let result = match command {
        DeploymentCommand::Create { spec } => {
            // Validate bounded local input before any provisioning request.
            let file = std::fs::File::open(spec)?;
            let mut bytes = Vec::new();
            use std::io::Read;
            file.take(1024 * 1024 + 1).read_to_end(&mut bytes)?;
            ensure!(bytes.len() <= 1024 * 1024, "specification exceeds 1 MiB");
            let spec: InferenceDeploymentSpec = serde_json::from_slice(&bytes)
                .map_err(|_| anyhow::anyhow!("invalid deployment specification JSON"))?;
            json!(provider.create(&spec).await?)
        }
        DeploymentCommand::Status { id } => {
            json!(provider.status(&provider.deployment_ref(&id)?).await?)
        }
        DeploymentCommand::Start {
            id,
            wait,
            timeout_seconds,
        } => {
            // Validate all arguments before making a billable mutation.
            let timeout = wait_timeout(timeout_seconds)?;
            let resource = provider.deployment_ref(&id)?;
            let deployment = provider.start(&resource).await?;
            if wait {
                json!(
                    provider
                        .wait_for(&resource, InferenceDeploymentState::Ready, timeout)
                        .await?
                )
            } else {
                json!(deployment)
            }
        }
        DeploymentCommand::Stop {
            id,
            wait,
            timeout_seconds,
        } => {
            let timeout = wait_timeout(timeout_seconds)?;
            let resource = provider.deployment_ref(&id)?;
            let deployment = provider.stop(&resource).await?;
            if wait {
                json!(
                    provider
                        .wait_for(&resource, InferenceDeploymentState::Stopped, timeout)
                        .await?
                )
            } else {
                json!(deployment)
            }
        }
        DeploymentCommand::Delete {
            id,
            wait,
            timeout_seconds,
        } => {
            let timeout = wait_timeout(timeout_seconds)?;
            let resource = provider.deployment_ref(&id)?;
            provider.delete(&resource).await?;
            if wait {
                json!(
                    provider
                        .wait_for(&resource, InferenceDeploymentState::Deleted, timeout)
                        .await?
                )
            } else {
                json!({"resource":resource,"deletion_requested":true})
            }
        }
        DeploymentCommand::Wait {
            id,
            state,
            timeout_seconds,
        } => {
            json!(
                provider
                    .wait_for(
                        &provider.deployment_ref(&id)?,
                        state.into(),
                        wait_timeout(timeout_seconds)?
                    )
                    .await?
            )
        }
    };
    println!("{}", serde_json::to_string_pretty(&result)?);
    Ok(())
}
