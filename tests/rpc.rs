#![cfg(feature = "plugin")]
use magicmtn_sdk::inference::ModelConfiguration;
use magicmtn_sdk::plugin::*;
#[cfg(not(feature = "bedrock-only"))]
use magicmtn_sdk::{InferenceDeploymentRef, InferenceDeploymentSpec};
use magicmtn_sdk::{InferenceRequest, ProviderSandbox};
use serde_json::json;
#[cfg(not(feature = "bedrock-only"))]
use std::path::Path;
#[cfg(not(feature = "bedrock-only"))]
use std::{
    io::Write,
    process::{Command, Stdio},
    time::Duration,
};

#[test]
#[cfg(not(feature = "bedrock-only"))]
fn all_three_traits_dispatch_over_one_persistent_process() {
    let mut child = Command::new(env!("CARGO_BIN_EXE_magicmtn-provider-aws"))
        .env(
            "MAGICMTN_AWS_PYTHON",
            concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/fake-python.py"),
        )
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let sandbox = ProviderSandbox {
        provider_id: "mvm-test".into(),
        status: None,
        create_time_ms: None,
        metadata: None,
    };
    let resource = InferenceDeploymentRef {
        provider_id: "sagemaker/test-model/models/org/model".into(),
    };
    let operations = vec![
        Operation::Describe,
        Operation::SandboxCreate {
            template: "image:example/agent:1|cee98b1a-b843-40f1-97d2-1bfc96c9c676".into(),
            ttl_seconds: None,
        },
        Operation::SandboxExecBounded {
            sandbox: sandbox.clone(),
            command: "echo".into(),
            args: vec!["literal;雪".into()],
            timeout_ms: 1000,
            max_output_bytes: 1000,
        },
        Operation::Infer {
            request: InferenceRequest {
                model: ModelConfiguration {
                    model: "endpoints/test-model/models/org/model".into(),
                    provider: Some("aws".into()),
                    version: None,
                    parameters: json!({"max_tokens":10}),
                },
                task: "chat".into(),
                input: json!({"messages":[{"role":"assistant","content":"","tool_calls":[{"function":{"name":"run","arguments":{"x":1}}}]}]}),
            },
        },
        Operation::InferenceDeploymentCreate {
            spec: InferenceDeploymentSpec {
                name: "test-model".into(),
                model: "org/model".into(),
                configuration: json!({}),
            },
        },
        Operation::InferenceDeploymentStart {
            resource: resource.clone(),
        },
        Operation::InferenceDeploymentStatus {
            resource: resource.clone(),
        },
        Operation::InferenceDeploymentStop {
            resource: resource.clone(),
        },
        Operation::InferenceDeploymentDelete { resource },
        Operation::SandboxTerminate {
            sandbox: sandbox.clone(),
        },
        Operation::SandboxSsh { sandbox },
        Operation::Shutdown,
    ];
    let mut stdin = child.stdin.take().unwrap();
    for (i, operation) in operations.into_iter().enumerate() {
        serde_json::to_writer(
            &mut stdin,
            &Request {
                id: i as u64,
                protocol_version: PROTOCOL_VERSION,
                provider: "aws".into(),
                operation,
            },
        )
        .unwrap();
        stdin.write_all(b"\n").unwrap();
    }
    drop(stdin);
    let result = child.wait_with_output().unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let responses: Vec<Response> = String::from_utf8(result.stdout)
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    assert_eq!(responses.len(), 12);
    for (i, r) in responses.iter().enumerate() {
        assert_eq!(r.id, i as u64);
        assert_eq!(r.provider, "aws");
        assert_eq!(r.protocol_version, PROTOCOL_VERSION);
        if i != 10 {
            assert!(matches!(r.outcome, Outcome::Ok(_)), "{:?}", r.outcome);
        }
    }
    let Outcome::Ok(Value::Inference(inference)) = &responses[3].outcome else {
        panic!("missing inference");
    };
    assert_eq!(inference.output["message"]["content"], "");
    assert_eq!(
        inference.output["message"]["tool_calls"][0]["function"]["arguments"],
        "{\"x\":1}"
    );
    let Outcome::Error(error) = &responses[10].outcome else {
        panic!("unsupported SSH expected");
    };
    assert_eq!(error.code, ErrorCode::UnsupportedOperation);
}

#[tokio::test]
#[cfg(not(feature = "bedrock-only"))]
async fn sdk_host_accepts_combined_manifest_without_python_or_credentials() {
    let client = magicmtn_provider_framework::host::Client::start(
        Path::new(env!("CARGO_BIN_EXE_magicmtn-provider-aws")),
        "aws",
    )
    .await
    .unwrap();
    assert_eq!(client.manifest.capabilities.inference_tasks, vec!["chat"]);
    assert_eq!(client.manifest.capabilities.sandbox_operations.len(), 4);
    assert_eq!(
        client
            .manifest
            .capabilities
            .inference_deployment_operations
            .len(),
        5
    );
    let sandbox = ProviderSandbox {
        provider_id: "mvm-test".into(),
        status: None,
        create_time_ms: None,
        metadata: None,
    };
    assert!(
        client
            .call(Operation::SandboxSsh { sandbox }, Duration::from_secs(1))
            .await
            .is_err()
    );
    client.shutdown().await;
}

#[tokio::test]
async fn rejects_invalid_inference_and_exec_before_starting_adapter() {
    use magicmtn_provider_aws::AwsProvider;
    use magicmtn_sdk::{InferenceBackend, SandboxBackend};
    let provider = AwsProvider::new("/this/python/does/not/exist");
    let request = InferenceRequest {
        model: ModelConfiguration {
            model: "endpoints/test-model/models/org/model".into(),
            provider: None,
            version: None,
            parameters: json!({"stream":true}),
        },
        task: "chat".into(),
        input: json!({"messages":[{"role":"user","content":"hi"}]}),
    };
    let error = provider.infer(&request).await.unwrap_err();
    assert_eq!(
        error.downcast_ref::<ProviderError>().unwrap().code,
        ErrorCode::InvalidRequest
    );
    let sandbox = ProviderSandbox {
        provider_id: "mvm-test".into(),
        status: None,
        create_time_ms: None,
        metadata: None,
    };
    assert!(
        provider
            .exec_bounded(&sandbox, "echo", &[], 0, 1)
            .await
            .unwrap_err()
            .to_string()
            .contains("limits")
    );
}

#[tokio::test]
async fn embedded_nitro_adapter_rejects_invalid_input_without_aws_dependencies() {
    use magicmtn_provider_aws::AwsProvider;
    use magicmtn_sdk::SandboxBackend;

    let provider = AwsProvider::new("python3");
    let error = provider
        .create("nitro-eks:example/agent:1|invalid-session")
        .await
        .expect_err("invalid Nitro session must fail before AWS access");
    let error = error
        .downcast_ref::<ProviderError>()
        .expect("embedded adapter must return a structured provider error");
    assert_eq!(error.code, ErrorCode::InvalidRequest);
    assert_eq!(error.message, "invalid Nitro session UUID");
}

#[tokio::test]
#[cfg(not(feature = "bedrock-only"))]
#[ignore = "requires ./package.sh to generate a release archive"]
async fn maintainer_archive_runs_with_standard_sdk_host() {
    let archive = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("dist")
        .join(format!(
            "aws-{}-{}-{}.tar.gz",
            magicmtn_provider_aws::VERSION,
            std::env::consts::OS,
            std::env::consts::ARCH,
        ));
    let root = std::env::temp_dir().join(format!(
        "magicmtn-aws-install-test-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    // Verify the archive before starting the SDK host.
    let status = std::process::Command::new(
        std::env::var("MAGICMTN_AWS_PYTHON").unwrap_or_else(|_| "python3".into()),
    )
    .args([
        "-c",
        r#"import hashlib, json, pathlib, sys, tarfile
archive, root, license_path = map(pathlib.Path, sys.argv[1:])
with tarfile.open(archive) as package:
    assert set(package.getnames()) == {"provider", "package.json", "LICENSE"}
    assert package.extractfile("LICENSE").read() == license_path.read_bytes()
    manifest = json.load(package.extractfile("package.json"))
    binary = package.extractfile("provider").read()
    assert hashlib.sha256(binary).hexdigest() == manifest["sha256"]
    assert manifest["name"] == "aws" and manifest["protocol_version"] == 1
    root.mkdir()
    (root / "package.json").write_text(json.dumps(manifest))
    (root / "provider").write_bytes(binary)
    mode = package.getmember("provider").mode
    assert mode & 0o111
    (root / "provider").chmod(mode)
"#,
    ])
    .arg(archive)
    .arg(&root)
    .arg(Path::new(env!("CARGO_MANIFEST_DIR")).join("LICENSE"))
    .status()
    .unwrap();
    assert!(status.success());
    let package: serde_json::Value =
        serde_json::from_slice(&std::fs::read(root.join("package.json")).unwrap()).unwrap();
    assert_eq!(package["version"], magicmtn_provider_aws::VERSION);
    assert_eq!(package["os"], std::env::consts::OS);
    assert_eq!(package["arch"], std::env::consts::ARCH);
    let client = magicmtn_provider_framework::host::Client::start(&root.join("provider"), "aws")
        .await
        .unwrap();
    assert_eq!(
        client.manifest.provider_version,
        magicmtn_provider_aws::VERSION
    );
    assert_eq!(client.manifest.capabilities.sandbox_operations.len(), 4);
    assert_eq!(
        client
            .manifest
            .capabilities
            .inference_deployment_operations
            .len(),
        5
    );
    assert_eq!(client.manifest.capabilities.inference_tasks, vec!["chat"]);
    client.shutdown().await;
    std::fs::remove_dir_all(root).unwrap();
}

#[tokio::test]
async fn bedrock_chat_preserves_tools_and_usage() {
    use magicmtn_sdk::InferenceBackend;
    let provider = magicmtn_provider_aws::AwsProvider::new(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/fake-python.py"
    ));
    let request = InferenceRequest {
        model: ModelConfiguration {
            model: "us.openai.gpt-6.1-sol".into(),
            provider: Some("aws".into()),
            version: None,
            parameters: json!({"max_tokens":100,"reasoning_effort":"low"}),
        },
        task: "chat".into(),
        input: json!({"messages":[{"role":"user","content":"hi"}]}),
    };
    let response = provider.infer(&request).await.unwrap();
    assert_eq!(response.output["usage"]["prompt_tokens"], 10);
    assert_eq!(
        response.output["message"]["tool_calls"][0]["function"]["arguments"],
        "{\"x\":1}"
    );
    let mut invalid = request.clone();
    invalid.model.parameters = json!({"max_tokens":1,"max_completion_tokens":2});
    assert!(provider.infer(&invalid).await.is_err());
}

#[test]
#[cfg(feature = "bedrock-only")]
fn bedrock_only_manifest_exposes_only_inference() {
    let manifest = magicmtn_provider_aws::capabilities();
    assert_eq!(manifest.inference_tasks, vec!["chat"]);
    assert!(manifest.sandbox_operations.is_empty());
    assert!(manifest.inference_deployment_operations.is_empty());
}

#[test]
#[cfg(feature = "bedrock-only")]
fn bedrock_only_rpc_discovers_and_rejects_non_bedrock_operations_without_python() {
    use std::io::Write;
    use std::process::{Command, Stdio};
    let mut child = Command::new(env!("CARGO_BIN_EXE_magicmtn-provider-aws"))
        .env_clear()
        .env("MAGICMTN_AWS_PYTHON", "/this/python/does/not/exist")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let operations = vec![
        Operation::Describe,
        Operation::SandboxCreate {
            template: "microvm:image-test|cee98b1a-b843-40f1-97d2-1bfc96c9c676".into(),
            ttl_seconds: None,
        },
        Operation::InferenceDeploymentStatus {
            resource: magicmtn_sdk::InferenceDeploymentRef {
                provider_id: "sagemaker/test-model/models/org/model".into(),
            },
        },
        Operation::Infer {
            request: InferenceRequest {
                model: ModelConfiguration {
                    model: "endpoints/test-model/models/org/model".into(),
                    provider: Some("aws".into()),
                    version: None,
                    parameters: json!({}),
                },
                task: "chat".into(),
                input: json!({"messages":[{"role":"user","content":"hi"}]}),
            },
        },
        Operation::Shutdown,
    ];
    let mut input = child.stdin.take().unwrap();
    for (id, operation) in operations.into_iter().enumerate() {
        let request = Request {
            id: id as u64,
            protocol_version: PROTOCOL_VERSION,
            provider: "aws".into(),
            operation,
        };
        writeln!(input, "{}", serde_json::to_string(&request).unwrap()).unwrap();
    }
    drop(input);
    let output = child.wait_with_output().unwrap();
    assert!(output.status.success());
    let responses: Vec<Response> = String::from_utf8(output.stdout)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(responses.len(), 5);
    let Outcome::Ok(Value::Description(manifest)) = &responses[0].outcome else {
        panic!("discovery failed");
    };
    assert_eq!(manifest.provider_version, magicmtn_provider_aws::VERSION);
    assert_eq!(manifest.capabilities.inference_tasks, vec!["chat"]);
    assert!(manifest.capabilities.sandbox_operations.is_empty());
    assert!(
        manifest
            .capabilities
            .inference_deployment_operations
            .is_empty()
    );
    for (response, expected) in responses[1..4].iter().zip([
        ErrorCode::UnsupportedOperation,
        ErrorCode::UnsupportedOperation,
        ErrorCode::InvalidRequest,
    ]) {
        let Outcome::Error(error) = &response.outcome else {
            panic!("restricted operation succeeded");
        };
        assert_eq!(error.code, expected);
        assert!(!error.retryable);
    }
    assert!(matches!(responses[4].outcome, Outcome::Ok(Value::Done)));
}
