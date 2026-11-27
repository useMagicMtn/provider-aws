//! Explicit connection setup for credential-aware SDK hosts.
use crate::AwsProvider;
use anyhow::ensure;
use magicmtn_sdk::{
    BoxFuture,
    provider_setup::{ConfigurationOption, ConfiguredConnection, ProviderSetup, SetupSpec},
};
use serde_json::json;
use std::collections::BTreeMap;

pub fn setup_spec() -> SetupSpec {
    let mut options = BTreeMap::from([
        (
            "region".into(),
            ConfigurationOption {
                description: "AWS region for account resources; Bedrock uses us-east-2".into(),
                default: Some("us-east-2".into()),
            },
        ),
        (
            "bedrock_role_arn".into(),
            ConfigurationOption {
                description: "IAM role assumed for Bedrock inference and profile verification"
                    .into(),
                default: None,
            },
        ),
    ]);
    if !cfg!(feature = "bedrock-only") {
        for (name, description, default) in [
            (
                "sandbox_backend",
                "Sandbox backend: microvm or nitro-eks",
                Some("microvm"),
            ),
            (
                "sandbox_platform",
                "Prepared sandbox platform: linux/arm64 or linux/amd64",
                None,
            ),
            (
                "microvm_images",
                "JSON map of OCI digest references to prepared MicroVM images",
                None,
            ),
            (
                "nitro_images",
                "JSON map of OCI digest references to prepared ECR image digests",
                None,
            ),
            (
                "nitro_eks_cluster",
                "Existing EKS cluster with the isolated-sandbox runtime class",
                None,
            ),
            (
                "nitro_eks_namespace",
                "Existing Kubernetes namespace for sandbox Jobs",
                Some("magicmtn-sandboxes"),
            ),
        ] {
            options.insert(
                name.into(),
                ConfigurationOption {
                    description: description.into(),
                    default: default.map(str::to_owned),
                },
            );
        }
    }
    SetupSpec { options }
}

impl ProviderSetup for AwsProvider {
    fn configure<'a>(
        &'a self,
        options: &'a BTreeMap<String, String>,
    ) -> BoxFuture<'a, ConfiguredConnection> {
        Box::pin(async move {
            setup_spec().validate_options(options)?;
            ensure!(
                self.credential.is_some(),
                "AWS setup requires explicit credentials"
            );
            let connection: ConfiguredConnection = serde_json::from_value(
                self.call(json!({"operation":"configure", "options":options, "bedrock_only":cfg!(feature = "bedrock-only")})).await?
            )?;
            connection.validate()?;
            ensure!(
                connection.account.len() == 12
                    && connection.account.bytes().all(|c| c.is_ascii_digit()),
                "invalid AWS setup account"
            );
            ensure!(
                connection
                    .configuration
                    .iter()
                    .all(|(key, _)| key.starts_with("MAGICMTN_AWS_")
                        && !key.ends_with("_PYTHON")
                        && !key.ends_with("_KUBECTL")),
                "invalid AWS setup setting"
            );
            Ok(connection)
        })
    }
    fn verify(&self) -> BoxFuture<'_, String> {
        Box::pin(self.verify_account())
    }
}
