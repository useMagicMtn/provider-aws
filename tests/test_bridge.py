import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
import threading
import urllib.request
from unittest.mock import patch, Mock

import boto3
from botocore.stub import Stubber
from botocore.exceptions import ClientError
from botocore.response import StreamingBody

ROOT = Path(__file__).resolve().parents[1]


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if path.name == "aws_bridge.py":
        exec(compile((path.parent / "nitro_eks.py").read_text(), "nitro_eks.py", "exec"), module.__dict__)
    return module


bridge = load(ROOT / "src/aws_bridge.py")
agent = load(ROOT / "sandbox/agent.py")
TAG = {"Key": "magicmtn-provider", "Value": "aws-v1"}
MODEL = "org/model"
REF = "sagemaker/test-model/models/" + MODEL
CFG_ARN = "arn:aws:sagemaker:us-east-1:123456789012:endpoint-config/test-model"
EP_ARN = "arn:aws:sagemaker:us-east-1:123456789012:endpoint/test-model"
MDL_ARN = "arn:aws:sagemaker:us-east-1:123456789012:model/test-model"


def api(service):
    return boto3.client(service, region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")


def spec(mode="serverless"):
    cfg = {"mode": mode, "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/chat:1", "execution_role_arn": "arn:aws:iam::123456789012:role/SageMaker"}
    if mode == "realtime":
        cfg["instance_type"] = "ml.g5.xlarge"
    return {"name": "test-model", "model": MODEL, "configuration": cfg}


class BridgeTests(unittest.TestCase):
    def test_bedrock_uses_invoke_model_and_us_profile(self):
        model = "us.openai.gpt-6.1-sol"
        body = {"model": model, "messages": [{"role":"user", "content":"hi"}], "stream": False}
        a = api("bedrock-runtime")
        payload = b'{"model":"test","choices":[]}'
        stream = StreamingBody(io.BytesIO(payload), len(payload))
        with Stubber(a) as stub, patch.object(bridge, "bedrock_client", return_value=a):
            stub.add_response("invoke_model", {"body": stream, "contentType":"application/json"},
                              {"modelId":model,"body":json.dumps(body).encode(),"contentType":"application/json","accept":"application/json"})
            self.assertEqual(bridge.dispatch({"operation":"infer", "target":model,"body":body})["model"], "test")
            stub.assert_no_pending_responses()
        self.assertTrue(stream._raw_stream.closed)

    def test_bedrock_assumes_only_configured_role_and_pins_runtime_endpoint(self):
        role = "arn:aws:iam::123456789012:role/magicmtn-bedrock-inference"
        source, assumed = Mock(), Mock()
        source.client.return_value.assume_role.return_value = {"Credentials": {
            "AccessKeyId":"temporary", "SecretAccessKey":"secret", "SessionToken":"token"}}
        with patch.dict(os.environ, {"AWS_REGION":"us-east-2", "MAGICMTN_AWS_BEDROCK_ROLE_ARN":role}), patch("boto3.Session", side_effect=[source, assumed]) as session:
            bridge.bedrock_client()
            source.client.return_value.assume_role.assert_called_once_with(RoleArn=role, RoleSessionName="magicmtn-bedrock", DurationSeconds=900)
            self.assertEqual(session.call_args.kwargs["aws_session_token"], "token")
            self.assertEqual(assumed.client.call_args.kwargs["endpoint_url"], "https://bedrock-runtime.us-east-2.amazonaws.com")
            self.assertEqual(assumed.client.call_args.kwargs["config"].retries, {"total_max_attempts":1})
        with patch.dict(os.environ, {"AWS_REGION":"eu-west-1"}), self.assertRaises(bridge.ProviderFailure):
            bridge.bedrock_client()

    def test_managed_bedrock_requires_explicit_role(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(bridge, "bedrock_client") as client:
            with self.assertRaises(bridge.ProviderFailure):
                bridge.bedrock_infer({"target":"us.openai.gpt-6-sol", "body":{"model":"us.openai.gpt-6-sol","stream":False}, "require_bedrock_role":True})
            client.assert_not_called()

    def test_bedrock_rejects_redirects_and_model_mismatch_before_network(self):
        for model in ("global.openai.gpt-6-sol", "us.anthropic.claude", "https://example.com"):
            with self.assertRaises(bridge.ProviderFailure):
                bridge.bedrock_infer({"target":model,"body":{"model":model,"stream":False}})
        with self.assertRaises(bridge.ProviderFailure):
            bridge.bedrock_infer({"target":"us.openai.gpt-6-sol","body":{"model":"other","stream":False}})
        with patch.dict(os.environ, {"MAGICMTN_AWS_BEDROCK_ROLE_ARN":"configured"}), self.assertRaises(bridge.ProviderFailure):
            bridge.dispatch({"operation":"infer","target":"endpoints/test/models/model","body":{}})

    def test_create_both_modes_uses_real_aws_shapes_without_serving_capacity(self):
        for mode in ("serverless", "realtime"):
            with self.subTest(mode=mode):
                a = api("sagemaker")
                tags = [TAG, {"Key": "magicmtn-model", "Value": MODEL}]
                variant = {"VariantName": "AllTraffic", "ModelName": "test-model"}
                if mode == "serverless":
                    variant["ServerlessConfig"] = {"MemorySizeInMB": 6144, "MaxConcurrency": 10}
                else:
                    variant.update(InstanceType="ml.g5.xlarge", InitialInstanceCount=1, InitialVariantWeight=1.0)
                with Stubber(a) as stub:
                    stub.add_response("create_model", {"ModelArn": MDL_ARN}, {"ModelName": "test-model", "ExecutionRoleArn": spec(mode)["configuration"]["execution_role_arn"], "PrimaryContainer": {"Image": spec(mode)["configuration"]["image"]}, "Tags": tags})
                    stub.add_response("create_endpoint_config", {"EndpointConfigArn": CFG_ARN}, {"EndpointConfigName": "test-model", "ProductionVariants": [variant], "Tags": tags})
                    result = bridge.create_deployment(spec(mode), a)
                    self.assertEqual(result["state"], "stopped")
                    self.assertEqual(result["inference_model"], REF)
                    stub.assert_no_pending_responses()

    def test_reject_mixed_config_before_mutation(self):
        for changes in ({"instance_type": "ml.g5.xlarge"}, {"memory_size_mb": 7000}, {"max_concurrency": True}, {"typo": 1}):
            s = spec()
            s["configuration"].update(changes)
            with self.assertRaises(bridge.ProviderFailure):
                bridge.create_deployment(s, None)

    def config(self, stub, tags=True):
        stub.add_response("describe_endpoint_config", {"EndpointConfigName": "test-model", "EndpointConfigArn": CFG_ARN,
            "ProductionVariants": [{"VariantName": "AllTraffic", "ModelName": "test-model", "ServerlessConfig": {"MemorySizeInMB": 6144, "MaxConcurrency": 10}}], "CreationTime": 0}, {"EndpointConfigName": "test-model"})
        stub.add_response("list_tags", {"Tags": [TAG] if tags else []}, {"ResourceArn": CFG_ARN})

    def no_endpoint(self, stub):
        stub.add_client_error("describe_endpoint", "ValidationException", "Could not find endpoint test-model", expected_params={"EndpointName": "test-model"})

    def endpoint(self, stub, state="InService"):
        stub.add_response("describe_endpoint", {"EndpointName": "test-model", "EndpointArn": EP_ARN, "EndpointConfigName": "test-model", "EndpointStatus": state,
            "CreationTime": 0, "LastModifiedTime": 0}, {"EndpointName": "test-model"})
        stub.add_response("list_tags", {"Tags": [TAG]}, {"ResourceArn": EP_ARN})

    def test_start_creates_endpoint(self):
        a = api("sagemaker")
        with Stubber(a) as stub, patch.object(bridge, "client", return_value=a):
            self.config(stub)
            self.no_endpoint(stub)
            stub.add_response("create_endpoint", {"EndpointArn": EP_ARN}, {"EndpointName": "test-model", "EndpointConfigName": "test-model", "Tags": [TAG, {"Key": "magicmtn-model", "Value": MODEL}]})
            self.assertEqual(bridge.manage({"operation": "deployment_start", "resource": {"provider_id": REF}})["state"], "starting")
            stub.assert_no_pending_responses()

    def test_stop_preserves_config_and_reports_pending_teardown(self):
        a = api("sagemaker")
        with Stubber(a) as stub, patch.object(bridge, "client", return_value=a):
            self.config(stub)
            self.endpoint(stub)
            stub.add_response("delete_endpoint", {}, {"EndpointName": "test-model"})
            self.assertEqual(bridge.manage({"operation": "deployment_stop", "resource": {"provider_id": REF}})["state"], "stopping")
            stub.assert_no_pending_responses()

    def test_delete_requires_endpoint_to_disappear(self):
        a = api("sagemaker")
        with Stubber(a) as stub, patch.object(bridge, "client", return_value=a):
            self.config(stub)
            self.endpoint(stub, "Deleting")
            with self.assertRaises(bridge.ProviderFailure):
                bridge.manage({"operation": "deployment_delete", "resource": {"provider_id": REF}})
            stub.assert_no_pending_responses()

    def test_delete_artifacts_after_stop(self):
        a = api("sagemaker")
        with Stubber(a) as stub, patch.object(bridge, "client", return_value=a):
            self.config(stub)
            self.no_endpoint(stub)
            stub.add_response("delete_endpoint_config", {}, {"EndpointConfigName": "test-model"})
            stub.add_response("describe_model", {"ModelName": "test-model", "ModelArn": MDL_ARN, "CreationTime": 0, "ExecutionRoleArn": spec()["configuration"]["execution_role_arn"]}, {"ModelName": "test-model"})
            stub.add_response("list_tags", {"Tags": [TAG]}, {"ResourceArn": MDL_ARN})
            stub.add_response("delete_model", {}, {"ModelName": "test-model"})
            self.assertIsNone(bridge.manage({"operation": "deployment_delete", "resource": {"provider_id": REF}}))
            stub.assert_no_pending_responses()

    def test_management_rejects_unowned_resources(self):
        a = api("sagemaker")
        with Stubber(a) as stub, patch.object(bridge, "client", return_value=a):
            self.config(stub, tags=False)
            with self.assertRaises(bridge.ProviderFailure):
                bridge.manage({"operation": "deployment_stop", "resource": {"provider_id": REF}})
            stub.assert_no_pending_responses()

    def test_missing_is_not_every_validation_error(self):
        self.assertFalse(bridge.missing(ClientError({"Error": {"Code": "ValidationException", "Message": "invalid name"}}, "DescribeEndpoint")))
        self.assertFalse(bridge.missing(ClientError({"Error": {"Code": "AccessDeniedException", "Message": "not found"}}, "DescribeEndpoint")))

    def test_microvm_run_idempotency_version_and_region(self):
        a = api("lambda-microvms")
        session = "cee98b1a-b843-40f1-97d2-1bfc96c9c676"
        expected = {"imageIdentifier": "image-test", "imageVersion": "1.0", "clientToken": session,
            "ingressNetworkConnectors": ["arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:ALL_INGRESS"],
            "idlePolicy": {"autoResumeEnabled": True, "maxIdleDurationSeconds": 900, "suspendedDurationSeconds": 28800}, "maximumDurationInSeconds": 28800}
        with Stubber(a) as stub, patch.object(bridge, "client", return_value=a), patch.dict(os.environ, {}, clear=True):
            stub.add_response("run_microvm", {"microvmId": "microvm-test", "state": "PENDING", "endpoint": "microvm-test.lambda-microvm.us-east-1.on.aws", "imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:image-test", "imageVersion": "1.0", "maximumDurationInSeconds": 28800, "startedAt": 0}, expected)
            r = bridge.dispatch({"operation": "sandbox_create", "template": "microvm:image-test@1.0|" + session})
            self.assertEqual(r["provider_id"], "microvm-test")
            self.assertEqual(r["metadata"]["region"], "us-east-1")
            self.assertNotIn("authToken", json.dumps(r))
            stub.assert_no_pending_responses()

    def test_microvm_requested_lifetime_and_provider_cap(self):
        session = "cee98b1a-b843-40f1-97d2-1bfc96c9c676"
        for requested, maximum in ((1800, 1800), (43200, 28800)):
            a = api("lambda-microvms")
            with Stubber(a) as stub, patch.object(bridge, "client", return_value=a), patch.dict(os.environ, {}, clear=True):
                stub.add_response("run_microvm", {"microvmId": "microvm-test", "state": "PENDING",
                    "endpoint": "microvm-test.lambda-microvm.us-east-1.on.aws",
                    "imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:image-test",
                    "imageVersion": "1.0", "maximumDurationInSeconds": maximum, "startedAt": 0},
                    {"imageIdentifier": "image-test", "imageVersion": "1.0", "clientToken": session,
                     "ingressNetworkConnectors": ["arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:ALL_INGRESS"],
                     "idlePolicy": {"autoResumeEnabled": True, "maxIdleDurationSeconds": 900, "suspendedDurationSeconds": 28800},
                     "maximumDurationInSeconds": maximum})
                bridge.dispatch({"operation": "sandbox_create", "template": "microvm:image-test@1.0|" + session,
                                 "ttl_seconds": requested})
                stub.assert_no_pending_responses()

    def test_microvm_lifecycle_actual_service_shapes(self):
        for op in ("terminate", "suspend", "resume"):
            a = api("lambda-microvms")
            with Stubber(a) as stub, patch.object(bridge, "client", return_value=a):
                stub.add_response(op + "_microvm", {}, {"microvmIdentifier": "microvm-test"})
                bridge.dispatch({"operation": op, "sandbox": {"provider_id": "microvm-test", "metadata": None}})
                stub.assert_no_pending_responses()

    def test_endpoint_rejects_credential_exfiltration(self):
        good = "microvm-test.lambda-microvm.us-east-1.on.aws"
        self.assertEqual(bridge.endpoint_url(good, "microvm-test", "us-east-1"), "https://" + good)
        live = "6ed09234-3611-dd2a-3052-24e37a16ea28.lambda-microvm.us-east-2.on.aws"
        self.assertEqual(bridge.endpoint_url(live, "microvm-test", "us-east-2"), "https://" + live)
        with self.assertRaises(bridge.ProviderFailure):
            bridge.endpoint_url(live, "microvm-test", "us-east-1")
        for bad in ("https://evil.com", "http://" + good, good + "/evil", good + ":8080", "user@" + good, good + "?a=1"):
            with self.assertRaises(bridge.ProviderFailure):
                bridge.endpoint_url(bad, "microvm-test", "us-east-1")

    def test_infer_invokes_endpoint_using_standard_runtime_api(self):
        a = api("sagemaker-runtime")
        raw = b'{"model":"org/model","choices":[]}'
        body = {"model": MODEL, "messages": [{"role": "user", "content": "hello"}], "stream": False}
        with Stubber(a) as stub, patch.object(bridge, "client", return_value=a):
            stub.add_response("invoke_endpoint", {"Body": StreamingBody(io.BytesIO(raw), len(raw)), "ContentType": "application/json"}, {"EndpointName": "test-model", "ContentType": "application/json", "Accept": "application/json", "Body": json.dumps(body).encode()})
            self.assertEqual(bridge.dispatch({"operation": "infer", "target": REF, "body": body})["model"], MODEL)
            stub.assert_no_pending_responses()

    def test_sensitive_error_body_never_disclosed(self):
        e = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "secret-prompt secret-token"}}, "RunMicrovm")
        with patch.object(bridge, "dispatch", side_effect=e), patch.object(bridge.sys, "stdin", type("Input", (), {"buffer": io.BytesIO(b'{}')})()), patch.object(bridge.sys, "stdout", new_callable=io.StringIO) as out:
            bridge.main()
            self.assertNotIn("secret", out.getvalue())
            self.assertIn("AccessDeniedException", out.getvalue())

    def test_exec_uses_fresh_aws_endpoint_port_scoped_token_and_one_post(self):
        a = api("lambda-microvms")
        raw = {"stdout": "ok", "stderr": "", "exit_code": 0, "elapsed_ms": 1, "timed_out": False, "truncated": False}
        opener = Mock()
        opener.open.side_effect = [io.BytesIO(b'{"agent_version":1}'), io.BytesIO(json.dumps(raw).encode())]
        with Stubber(a) as stub, patch.object(bridge, "client", return_value=a), patch.object(bridge.urllib.request, "build_opener", return_value=opener):
            stub.add_response("get_microvm", {"microvmId": "microvm-test", "state": "RUNNING", "endpoint": "microvm-test.lambda-microvm.us-east-1.on.aws", "imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:image-test", "imageVersion": "1.0", "maximumDurationInSeconds": 28800, "startedAt": 0}, {"microvmIdentifier": "microvm-test"})
            stub.add_response("create_microvm_auth_token", {"authToken": {"X-aws-proxy-auth": "secret-token"}}, {"microvmIdentifier": "microvm-test", "expirationInMinutes": 1, "allowedPorts": [{"port": 8080}]})
            r = bridge.dispatch({"operation": "exec", "sandbox": {"provider_id": "microvm-test", "metadata": {"endpoint": "https://evil.com"}}, "command": "echo", "args": [], "timeout_ms": 1000, "max_output_bytes": 100})
            self.assertEqual(r, raw)
            calls = [call.args[0] for call in opener.open.call_args_list]
            self.assertEqual([r.get_method() for r in calls], ["GET", "POST"])
            self.assertEqual(calls[1].full_url, "https://microvm-test.lambda-microvm.us-east-1.on.aws/exec")
            self.assertEqual(calls[1].get_header("X-aws-proxy-auth"), "secret-token")
            self.assertNotIn("secret-token", json.dumps(r))
            stub.assert_no_pending_responses()

    def test_session_oci_mapping_requires_explicit_host_configuration(self):
        request = {"operation": "sandbox_create", "template": "image:example/agent:1|cee98b1a-b843-40f1-97d2-1bfc96c9c676"}
        with patch.dict(os.environ, {}, clear=True), patch.object(bridge, "client") as constructor:
            with self.assertRaises(bridge.ProviderFailure):
                bridge.dispatch(request)
            constructor.assert_not_called()
        a = api("lambda-microvms")
        with patch.dict(os.environ, {"MAGICMTN_AWS_MICROVM_IMAGES": '{"example/agent:1":"image-test@1.0"}'}, clear=True), patch.object(bridge, "client", return_value=a), patch.object(a, "run_microvm", return_value={"microvmId": "microvm-test", "state": "PENDING"}) as run:
            r = bridge.dispatch(request)
            self.assertEqual(run.call_args.kwargs["imageIdentifier"], "image-test")
            self.assertEqual(run.call_args.kwargs["imageVersion"], "1.0")
            self.assertEqual(r["metadata"]["source_image"], "example/agent:1")

    def test_status_reports_stopped_only_after_endpoint_disappears(self):
        a = api("sagemaker")
        with Stubber(a) as stub, patch.object(bridge, "client", return_value=a):
            self.config(stub)
            self.no_endpoint(stub)
            stub.add_response("describe_model", {"ModelName": "test-model", "ModelArn": MDL_ARN, "CreationTime": 0, "ExecutionRoleArn": spec()["configuration"]["execution_role_arn"]}, {"ModelName": "test-model"})
            stub.add_response("list_tags", {"Tags": [TAG]}, {"ResourceArn": MDL_ARN})
            result = bridge.manage({"operation": "deployment_status", "resource": {"provider_id": REF}})
            self.assertEqual(result["state"], "stopped")
            self.assertEqual(result["ready_replicas"], 0)
            stub.assert_no_pending_responses()

    def test_sdk_retry_disabled(self):
        with patch.dict(os.environ, {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test", "AWS_DEFAULT_REGION": "us-east-1", "AWS_ENDPOINT_URL": "http://127.0.0.1:12345"}):
            a = bridge.client("lambda-microvms")
            self.assertEqual(a.meta.config.retries["total_max_attempts"], 1)
            self.assertEqual(a.meta.endpoint_url, "https://lambda.us-east-1.amazonaws.com")


class AgentTests(unittest.TestCase):
    def test_aws_build_hooks_accept_post(self):
        server = agent.ThreadingHTTPServer(("127.0.0.1", 0), agent.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for hook in ("ready", "validate"):
                url = f"http://127.0.0.1:{server.server_port}/aws/lambda-microvms/runtime/v1/{hook}"
                with urllib.request.urlopen(urllib.request.Request(url, data=b"{}")) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.load(response)["agent_version"], 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def run_command(self, program, args, timeout=1000, limit=1000):
        with tempfile.TemporaryDirectory() as cwd:
            return agent.execute({"command": program, "args": args, "timeout_ms": timeout, "max_output_bytes": limit}, cwd=cwd)

    def test_literal_arguments_and_nonzero_exit(self):
        r = self.run_command("/usr/bin/printf", ["%s", "$(echo injected);雪"])
        self.assertEqual(r["stdout"], "$(echo injected);雪")
        self.assertEqual(r["exit_code"], 0)
        r = self.run_command("/bin/sh", ["-c", "echo err >&2; exit 7"])
        self.assertEqual(r["exit_code"], 7)
        self.assertEqual(r["stderr"], "err\n")

    def test_timeout_kills_process_group(self):
        r = self.run_command("/bin/sh", ["-c", "sleep 10 & wait"], timeout=50)
        self.assertTrue(r["timed_out"])
        self.assertLess(r["elapsed_ms"], 1500)

    def test_noisy_output_is_drained_and_bounded(self):
        r = self.run_command(sys.executable, ["-c", "import os; exec(\"while True: os.write(1, b\'noise\' * 1000)\")"], timeout=100, limit=16)
        self.assertTrue(r["timed_out"])
        self.assertTrue(r["truncated"])
        self.assertLessEqual(len(r["stdout"].encode()) + len(r["stderr"].encode()), 16)

    def test_background_child_cannot_hold_pipes_open(self):
        r = self.run_command("/bin/sh", ["-c", "sleep 10 & echo done"], timeout=200)
        self.assertEqual(r["stdout"], "done\n")
        self.assertLess(r["elapsed_ms"], 1500)

    def test_validation(self):
        for bad in ({}, {"command": "echo", "args": [], "timeout_ms": True, "max_output_bytes": 1}):
            with self.assertRaises(ValueError):
                agent.validate(bad)


if __name__ == "__main__":
    unittest.main()
