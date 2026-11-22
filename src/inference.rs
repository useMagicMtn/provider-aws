//! AWS text chat translation using MagicMtn task schemas.
use crate::AwsProvider;
use anyhow::{Context, Result, bail, ensure};
use magicmtn_sdk::{InferenceRequest, InferenceResponse};
use serde_json::{Value, json};
impl AwsProvider {
    pub(crate) async fn infer_inner(
        &self,
        request: &InferenceRequest,
    ) -> Result<InferenceResponse> {
        ensure!(
            request.model.provider.as_deref().is_none_or(|p| p == "aws"),
            "incorrect inference provider"
        );
        ensure!(
            !request.model.model.trim().is_empty(),
            "model name required"
        );
        ensure!(
            request.model.version.is_none(),
            "put the serving version in the model name"
        );
        let bedrock = request.model.model.starts_with("us.openai.");
        let served_model = if bedrock {
            ensure!(
                request
                    .model
                    .model
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || b".-:".contains(&c)),
                "invalid Bedrock profile"
            );
            request.model.model.as_str()
        } else {
            ensure!(
                !cfg!(feature = "bedrock-only"),
                "this build requires a US Bedrock OpenAI inference profile"
            );
            let parts: Vec<_> = request.model.model.splitn(4, '/').collect();
            ensure!(
                parts.len() == 4
                    && matches!(parts[0], "endpoints" | "sagemaker")
                    && parts[2] == "models"
                    && !parts[3].is_empty(),
                "invalid SageMaker reference"
            );
            parts[3]
        };
        let mut body = if request.model.parameters.is_null() {
            json!({})
        } else {
            request.model.parameters.clone()
        };
        let parameters = body
            .as_object_mut()
            .context("model parameters must be an object")?;
        for key in ["model", "messages", "tools", "input", "stream", "n"] {
            ensure!(
                !parameters.contains_key(key),
                "model parameters cannot override {key}"
            );
        }
        let allowed: &[&str] = match request.task.as_str() {
            "chat" => &[
                "temperature",
                "top_p",
                "top_k",
                "min_p",
                "max_tokens",
                "max_completion_tokens",
                "stop",
                "presence_penalty",
                "frequency_penalty",
                "repetition_penalty",
                "seed",
                "response_format",
                "tool_choice",
                "parallel_tool_calls",
                "reasoning_effort",
            ],
            _ => return Err(crate::unsupported("unsupported AWS inference task")),
        };
        ensure!(
            parameters.keys().all(|key| allowed.contains(&key.as_str())),
            "unsupported AWS inference parameter"
        );
        if bedrock {
            ensure!(
                !parameters.contains_key("max_tokens")
                    || !parameters.contains_key("max_completion_tokens"),
                "conflicting token limits"
            );
            if let Some(limit) = parameters.remove("max_tokens") {
                parameters.insert("max_completion_tokens".into(), limit);
            }
        }
        body["model"] = json!(served_model);
        match request.task.as_str() {
            "chat" => {
                ensure!(
                    request.input.as_object().is_some_and(|o| o
                        .keys()
                        .all(|k| matches!(k.as_str(), "messages" | "tools"))),
                    "unsupported chat input field"
                );
                let input = magicmtn_sdk::chat::normalize_input(&request.input)?;
                let mut messages = input["messages"]
                    .as_array()
                    .context("chat messages required")?
                    .clone();
                ensure!(!messages.is_empty(), "chat messages required");
                for message in &mut messages {
                    if message["role"] == "tool" {
                        ensure!(
                            message["tool_call_id"]
                                .as_str()
                                .is_some_and(|id| !id.is_empty()),
                            "tool result requires a matching call ID"
                        );
                    }
                    if let Some(calls) = message.get_mut("tool_calls").and_then(Value::as_array_mut)
                    {
                        for call in calls {
                            call["function"].as_object_mut().unwrap().remove("index");
                        }
                    }
                }
                body["messages"] = json!(messages);
                if let Some(tools) = input.get("tools") {
                    ensure!(tools.is_array(), "tools must be an array");
                    if !tools.as_array().unwrap().is_empty() {
                        body["tools"] = tools.clone();
                    }
                }
                body["stream"] = json!(false);
            }
            task => bail!("unsupported inference task {task}"),
        };
        let raw = self
            .call(json!({"operation":"infer","target":request.model.model,"body":body,"require_bedrock_role":cfg!(feature = "bedrock-only")}))
            .await?;
        let decoded = (|| -> Result<InferenceResponse> {
            let model = raw["model"]
                .as_str()
                .context("inference response model missing")?
                .to_owned();
            let output = {
                let choices = raw["choices"].as_array().context("chat choices required")?;
                ensure!(choices.len() == 1, "exactly one chat choice required");
                let mut message = magicmtn_sdk::chat::normalize_message(
                    &choices[0]["message"],
                    &format!(
                        "call_{}",
                        std::time::SystemTime::now()
                            .duration_since(std::time::UNIX_EPOCH)?
                            .as_nanos()
                    ),
                )?;
                // Strip upstream metadata and expose the SDK function-call shape to the
                // harness while preserving reasoning_content for later turns.
                message.as_object_mut().unwrap().remove("tools");
                if let Some(calls) = message.get_mut("tool_calls").and_then(Value::as_array_mut) {
                    for call in calls {
                        *call = json!({"id":call["id"],"type":"function",
                            "function":{"name":call["function"]["name"],
                                "arguments":call["function"]["arguments"]}});
                    }
                }
                ensure!(
                    message["role"] == "assistant",
                    "assistant response required"
                );
                ensure!(
                    message["content"].is_string(),
                    "assistant content must be text"
                );
                let finish = choices[0]["finish_reason"]
                    .as_str()
                    .context("chat finish reason required")?;
                ensure!(finish != "error", "AWS failed during generation");
                json!({"message":message,"usage":raw.get("usage").cloned().unwrap_or(json!({})),"finish_reason":finish})
            };
            Ok(InferenceResponse { model, output })
        })();
        decoded.map_err(|_| crate::upstream("AWS returned an invalid inference response"))
    }
}
