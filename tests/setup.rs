#![cfg(feature = "plugin")]
#[cfg(feature = "bedrock-only")]
use magicmtn_sdk::plugin::ErrorCode;
use magicmtn_sdk::plugin::{
    CREDENTIAL_PROTOCOL_VERSION, Operation, Outcome, Request, Response, Value,
};
use serde_json::json;
use std::{
    io::Write,
    process::{Command, Stdio},
};

fn run(operations: Vec<Operation>, credentialed: bool, python: &str) -> Vec<Response> {
    let mut child = Command::new(env!("CARGO_BIN_EXE_magicmtn-provider-aws"))
        .env_clear()
        .env("MAGICMTN_AWS_PYTHON", python)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let credential = credentialed
        .then(|| json!({"AWS_ACCESS_KEY_ID":"test-key","AWS_SECRET_ACCESS_KEY":"test-secret"}));
    let mut stdin = child.stdin.take().unwrap();
    writeln!(stdin,"{}", json!({"id":1,"provider":"aws","protocol_version":CREDENTIAL_PROTOCOL_VERSION,"credential":credential,"verify_account":false})).unwrap();
    for (index, operation) in operations.into_iter().enumerate() {
        serde_json::to_writer(
            &mut stdin,
            &Request {
                id: index as u64 + 2,
                protocol_version: CREDENTIAL_PROTOCOL_VERSION,
                provider: "aws".into(),
                operation,
            },
        )
        .unwrap();
        writeln!(stdin).unwrap();
    }
    drop(stdin);
    let output = child.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(!String::from_utf8_lossy(&output.stdout).contains("test-secret"));
    assert!(!String::from_utf8_lossy(&output.stderr).contains("test-secret"));
    String::from_utf8(output.stdout)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}

#[test]
fn credential_free_discovery_advertises_setup_and_selectable_models() {
    let result = run(vec![], false, "/missing/python");
    assert_eq!(result.len(), 1);
    let Outcome::Ok(Value::Description(manifest)) = &result[0].outcome else {
        panic!("missing manifest")
    };
    let setup = manifest.capabilities.setup.as_ref().unwrap();
    assert!(setup.options.contains_key("region"));
    assert!(setup.options.contains_key("bedrock_role_arn"));
    assert_eq!(
        setup.options.contains_key("sandbox_backend"),
        !cfg!(feature = "bedrock-only")
    );
    assert_eq!(
        manifest.capabilities.inference_models[0].id,
        "us.openai.gpt-6.1-sol"
    );
    assert_eq!(
        manifest.capabilities.inference_models[0].context_window,
        1_000_000
    );
    assert_eq!(
        manifest.capabilities.inference_models[0].max_tokens,
        131_072
    );
}

#[test]
fn explicit_setup_uses_the_shared_configured_connection_contract() {
    let result = run(
        vec![
            Operation::ProviderConfigure {
                options: [("region".into(), "us-east-2".into())].into(),
            },
            Operation::ProviderConfigure {
                options: [("unknown".into(), "value".into())].into(),
            },
            Operation::Shutdown,
        ],
        true,
        concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/fake-python.py"),
    );
    assert_eq!(result.len(), 4);
    let Outcome::Ok(Value::ConfiguredConnection(connection)) = &result[1].outcome else {
        panic!("missing connection")
    };
    assert_eq!(connection.account, "123456789012");
    connection.validate().unwrap();
    assert_eq!(connection.configuration["MAGICMTN_AWS_REGION"], "us-east-2");
    assert!(
        connection
            .configuration
            .keys()
            .all(|key| key.starts_with("MAGICMTN_AWS_"))
    );
    assert!(matches!(result[2].outcome, Outcome::Error(_)));
    assert!(matches!(result[3].outcome, Outcome::Ok(Value::Done)));
}

#[test]
#[cfg(not(feature = "bedrock-only"))]
fn prepared_images_status_and_serverless_selection_dispatch_over_v2() {
    let result = run(
        vec![
            Operation::SandboxPrepareImage {
                images: [(
                    "linux/arm64".into(),
                    magicmtn_sdk::image_registry::Image {
                        repository: "example/canonical".into(),
                        digest: format!("sha256:{}", "a".repeat(64)),
                    },
                )]
                .into(),
            },
            Operation::SandboxStatus {
                sandbox: magicmtn_sdk::ProviderSandbox {
                    provider_id: "mvm-test".into(),
                    status: None,
                    create_time_ms: None,
                    metadata: None,
                },
            },
            Operation::InferenceDeploymentCreate {
                spec: magicmtn_sdk::InferenceDeploymentSpec {
                    name: "selection".into(),
                    model: "us.openai.gpt-6.1-sol".into(),
                    configuration: json!({}),
                },
            },
            Operation::Shutdown,
        ],
        true,
        concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/fake-python.py"),
    );
    assert_eq!(result.len(), 5);
    let Outcome::Ok(Value::PreparedImage(image)) = &result[1].outcome else {
        panic!("missing prepared image")
    };
    assert_eq!(image.platform, "linux/arm64");
    assert!(image.artifact.ends_with("@1.0"));
    assert!(matches!(
        result[2].outcome,
        Outcome::Ok(Value::SandboxStatus(magicmtn_sdk::SandboxState::Running))
    ));
    let Outcome::Ok(Value::InferenceDeployment(deployment)) = &result[3].outcome else {
        panic!("missing deployment")
    };
    assert_eq!(deployment.inference_model, "us.openai.gpt-6.1-sol");
    assert_eq!(
        deployment.state,
        magicmtn_sdk::InferenceDeploymentState::Ready
    );
    assert!(
        deployment
            .resource
            .provider_id
            .starts_with("bedrock/123456789012/")
    );
}

#[test]
#[cfg(feature = "bedrock-only")]
fn bedrock_only_gates_sandbox_extensions_without_starting_python() {
    let result = run(
        vec![
            Operation::SandboxPrepareImage {
                images: Default::default(),
            },
            Operation::SandboxStatus {
                sandbox: magicmtn_sdk::ProviderSandbox {
                    provider_id: "mvm-test".into(),
                    status: None,
                    create_time_ms: None,
                    metadata: None,
                },
            },
            Operation::Shutdown,
        ],
        true,
        "/missing/python",
    );
    for response in &result[1..3] {
        let Outcome::Error(error) = &response.outcome else {
            panic!("unsupported operation succeeded")
        };
        assert_eq!(error.code, ErrorCode::UnsupportedOperation);
    }
}
