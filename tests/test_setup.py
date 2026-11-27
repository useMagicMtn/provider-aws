"""Current SDK setup, image preparation, and status contracts without AWS resources."""
import copy
import json
import os
import unittest
from unittest.mock import Mock, patch

from botocore.stub import Stubber
from botocore.exceptions import ClientError
from test_bridge import api, bridge
from test_nitro import connection, job_fixture, pod_fixture, SANDBOX, UID

ACCOUNT = "123456789012"
PROFILE = "us.openai.gpt-6.1-sol"
IMAGE = {"repository": "ghcr.io/usemagicmtn/canonical", "digest": "sha256:" + "a" * 64}
REFERENCE = IMAGE["repository"] + "@" + IMAGE["digest"]
IMAGE_ARN = "arn:aws:lambda:us-east-1:123456789012:microvm-image:image-test"


def version():
    return {"baseImageArn": "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1",
            "buildRoleArn": "arn:aws:iam::123456789012:role/build", "codeArtifact": {"uri": "s3://test/agent.zip"},
            "imageArn": IMAGE_ARN, "imageVersion": "1.0", "state": "SUCCESSFUL", "status": "ACTIVE",
            "createdAt": 0, "cpuConfigurations": [{"architecture": "ARM_64"}]}


class SetupTests(unittest.TestCase):
    def test_setup_verifies_account_without_allocating_and_returns_scoped_settings(self):
        sts = api("sts")
        credential = {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "secret"}
        with patch.dict(os.environ, {}, clear=True), patch.object(bridge, "CREDENTIAL", credential), patch.object(bridge, "client", return_value=sts) as factory, Stubber(sts) as stub:
            stub.add_response("get_caller_identity", {"Account": ACCOUNT, "Arn": "arn:aws:iam::123456789012:user/test", "UserId": "test"}, {})
            configured = bridge.dispatch({"operation": "configure", "options": {"region": "us-east-1"}})
            self.assertEqual(configured["account"], ACCOUNT)
            self.assertEqual(configured["configuration"]["MAGICMTN_AWS_REGION"], "us-east-1")
            self.assertEqual(configured["configuration"]["MAGICMTN_AWS_BEDROCK_REGION"], "us-east-2")
            self.assertEqual(configured["configuration"]["MAGICMTN_AWS_ACCOUNT_ID"], ACCOUNT)
            self.assertEqual(configured["configuration"]["MAGICMTN_AWS_SANDBOX_PLATFORM"], "linux/arm64")
            self.assertTrue(all(key.startswith("MAGICMTN_AWS_") for key in configured["configuration"]))
            self.assertNotIn("secret", json.dumps(configured))
            self.assertNotIn("MAGICMTN_AWS_REGION", os.environ)
            factory.assert_called_once_with("sts", connect_timeout=3, read_timeout=5)
            stub.assert_no_pending_responses()

    def test_invalid_options_and_identity_replacement_fail_before_aws(self):
        with patch.dict(os.environ, {"MAGICMTN_AWS_REGION": "us-east-1"}, clear=True), patch.object(bridge, "CREDENTIAL", {}), patch.object(bridge, "client") as factory:
            for options in ({"unknown": "value"}, {"region": "us-west-2"}, {"microvm_images": "invalid"}, {"region": 4}):
                with self.subTest(options=options), self.assertRaises(bridge.ProviderFailure):
                    bridge.configure_connection({"options": options})
            factory.assert_not_called()

    def test_verified_account_drift_is_rejected(self):
        sts = Mock(); sts.get_caller_identity.return_value = {"Account": "999999999999"}
        with patch.dict(os.environ, {"MAGICMTN_AWS_ACCOUNT_ID": ACCOUNT}, clear=True), patch.object(bridge, "CREDENTIAL", {}), patch.object(bridge, "client", return_value=sts):
            with self.assertRaisesRegex(bridge.ProviderFailure, "identity changed"):
                bridge.verify_connection()

    def test_nitro_setup_selects_existing_namespace_and_runtime(self):
        nitro = connection(); nitro.ca_sha256 = "a" * 64; nitro.cluster_arn = "arn:aws:eks:us-east-1:123456789012:cluster/test-cluster"
        nitro.request = Mock(side_effect=[{"metadata": {"name": nitro.namespace, "uid": UID}}, {"metadata": {"name": "isolated-sandbox", "uid": UID}}])
        with patch.dict(os.environ, {}, clear=True), patch.object(bridge, "CREDENTIAL", {}), patch.object(bridge, "caller_account", return_value=ACCOUNT), patch.object(bridge, "NitroEks", return_value=nitro):
            configured = bridge.configure_connection({"options": {"region": "us-east-1", "sandbox_backend": "nitro-eks", "nitro_eks_cluster": "test-cluster"}})
            self.assertEqual(configured["configuration"]["MAGICMTN_AWS_NITRO_EKS_CLUSTER_ARN"], nitro.cluster_arn)
            self.assertEqual(configured["configuration"]["MAGICMTN_AWS_SANDBOX_PLATFORM"], "linux/amd64")
            self.assertTrue(all(call.args[0] == "GET" for call in nitro.request.call_args_list))

    def test_microvm_preparation_pins_an_existing_registered_version(self):
        aws = api("lambda-microvms")
        mapping = {REFERENCE: "image-test@1.0", "example/legacy:1": "image-legacy@1.0"}
        with patch.dict(os.environ, {"MAGICMTN_AWS_MICROVM_IMAGES": json.dumps(mapping)}, clear=True), patch.object(bridge, "caller_account", return_value=ACCOUNT), patch.object(bridge, "client", return_value=aws), Stubber(aws) as stub:
            stub.add_response("get_microvm_image_version", version(), {"imageIdentifier": "image-test", "imageVersion": "1.0"})
            prepared = bridge.prepare_aws_image({"linux/arm64": IMAGE})
            self.assertEqual(prepared["image"], IMAGE)
            self.assertEqual(prepared["platform"], "linux/arm64")
            self.assertEqual(prepared["artifact"], IMAGE_ARN + "@1.0")
            saved = json.loads(prepared["configuration"]["MAGICMTN_AWS_MICROVM_IMAGES"])
            self.assertEqual(saved[REFERENCE], IMAGE_ARN + "@1.0")
            self.assertEqual(saved["example/legacy:1"], mapping["example/legacy:1"])
            stub.assert_no_pending_responses()

    def test_unversioned_microvm_mapping_resolves_once_to_an_immutable_version(self):
        aws = api("lambda-microvms")
        with patch.dict(os.environ, {"MAGICMTN_AWS_MICROVM_IMAGES": json.dumps({REFERENCE: "image-test"})}, clear=True), patch.object(bridge, "caller_account", return_value=ACCOUNT), patch.object(bridge, "client", return_value=aws), Stubber(aws) as stub:
            stub.add_response("get_microvm_image", {"imageArn": IMAGE_ARN, "name": "image-test", "state": "CREATED", "latestActiveImageVersion": "1.0", "createdAt": 0}, {"imageIdentifier": "image-test"})
            stub.add_response("get_microvm_image_version", version(), {"imageIdentifier": "image-test", "imageVersion": "1.0"})
            self.assertEqual(bridge.prepare_aws_image({"linux/arm64": IMAGE})["artifact"], IMAGE_ARN + "@1.0")
            stub.assert_no_pending_responses()

    def test_bad_or_unmapped_digests_fail_before_clients(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(bridge, "client") as factory:
            for images in ({}, {"linux/amd64": IMAGE}, {"linux/arm64": dict(IMAGE, digest="latest")}, {"linux/arm64": IMAGE}):
                with self.subTest(images=images), self.assertRaises(bridge.ProviderFailure):
                    bridge.prepare_aws_image(images)
            factory.assert_not_called()

    def test_microvm_preparation_rejects_changed_account_version_and_architecture(self):
        for change in ({"imageArn": IMAGE_ARN.replace(ACCOUNT, "999999999999")}, {"imageVersion": "2.0"}, {"cpuConfigurations": []}, {"status": "INACTIVE"}):
            response = dict(version(), **change)
            aws = Mock(); aws.meta.partition = "aws"; aws.meta.region_name = "us-east-1"
            aws.get_microvm_image_version.return_value = response
            with patch.dict(os.environ, {"MAGICMTN_AWS_MICROVM_IMAGES": json.dumps({REFERENCE: "image-test@1.0"})}, clear=True), patch.object(bridge, "caller_account", return_value=ACCOUNT), patch.object(bridge, "client", return_value=aws):
                with self.subTest(change=change), self.assertRaises(bridge.ProviderFailure):
                    bridge.prepare_aws_image({"linux/arm64": IMAGE})

    def test_nitro_preparation_checks_the_exact_ecr_digest_read_only(self):
        aws = api("ecr"); digest = "sha256:" + "b" * 64
        target = f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/prepared/canonical@{digest}"
        with patch.dict(os.environ, {"MAGICMTN_AWS_REGION": "us-east-1", "MAGICMTN_AWS_SANDBOX_BACKEND": "nitro-eks", "MAGICMTN_AWS_NITRO_IMAGES": json.dumps({REFERENCE: target})}, clear=True), patch.object(bridge, "caller_account", return_value=ACCOUNT), patch.object(bridge, "client", return_value=aws), Stubber(aws) as stub:
            stub.add_response("describe_images", {"imageDetails": [{"registryId": ACCOUNT, "repositoryName": "prepared/canonical", "imageDigest": digest}]}, {"registryId": ACCOUNT, "repositoryName": "prepared/canonical", "imageIds": [{"imageDigest": digest}]})
            result = bridge.prepare_aws_image({"linux/amd64": IMAGE})
            self.assertEqual(result["artifact"], target)
            self.assertEqual(json.loads(result["configuration"]["MAGICMTN_AWS_NITRO_IMAGES"])[REFERENCE], target)
            stub.assert_no_pending_responses()

    def test_bedrock_selection_reads_a_profile_without_provisioning_sagemaker(self):
        control = Mock(); control.get_inference_profile.return_value = {
            "inferenceProfileId": PROFILE, "inferenceProfileArn": f"arn:aws:bedrock:us-east-2:{ACCOUNT}:inference-profile/{PROFILE}", "status": "ACTIVE"}
        with patch.dict(os.environ, {}, clear=True), patch.object(bridge, "caller_account", return_value=ACCOUNT), patch.object(bridge, "bedrock_client", return_value=control) as factory, patch.object(bridge, "manage") as manage:
            selected = bridge.dispatch({"operation": "deployment_create", "spec": {"name": "model-selection", "model": PROFILE, "configuration": {}}})
            self.assertEqual(selected["state"], "ready")
            self.assertEqual(selected["inference_model"], PROFILE)
            self.assertEqual(selected["resource"]["provider_id"], f"bedrock/{ACCOUNT}/us-east-2/profiles/{PROFILE}")
            factory.assert_called_once_with("bedrock")
            control.get_inference_profile.assert_called_once_with(inferenceProfileIdentifier=PROFILE)
            manage.assert_not_called()
            deleted = bridge.dispatch({"operation": "deployment_delete", "resource": selected["resource"]})
            self.assertIsNone(deleted)
            self.assertEqual(control.get_inference_profile.call_count, 1)
            with self.assertRaises(bridge.ProviderFailure) as failure:
                bridge.dispatch({"operation": "deployment_stop", "resource": selected["resource"]})
            self.assertEqual(failure.exception.code, "unsupported_operation")

    def test_bedrock_selection_rejects_cross_account_and_profile_identity_changes(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(bridge, "caller_account", return_value=ACCOUNT), patch.object(bridge, "bedrock_client") as factory:
            with self.assertRaises(bridge.ProviderFailure):
                bridge.bedrock_selection({"operation": "deployment_status", "resource": {"provider_id": f"bedrock/999999999999/us-east-2/profiles/{PROFILE}"}})
            factory.assert_not_called()
            factory.return_value.get_inference_profile.return_value = {"inferenceProfileId": PROFILE, "status": "ACTIVE", "inferenceProfileArn": "different"}
            with self.assertRaises(bridge.ProviderFailure):
                bridge.bedrock_selection({"operation": "deployment_create", "spec": {"model": PROFILE, "configuration": {}}})

    def test_microvm_status_is_authoritative_and_never_converts_outages_to_absence(self):
        aws = api("lambda-microvms")
        base = {"microvmId": "microvm-test", "endpoint": "microvm-test.lambda-microvm.us-east-1.on.aws", "imageArn": IMAGE_ARN, "imageVersion": "1.0", "maximumDurationInSeconds": 28800, "startedAt": 0}
        request = {"operation": "sandbox_status", "sandbox": {"provider_id": "microvm-test"}}
        with patch.object(bridge, "client", return_value=aws), Stubber(aws) as stub:
            for state, expected in (("RUNNING", "running"), ("SUSPENDED", "stopped"), ("TERMINATED", "missing")):
                stub.add_response("get_microvm", dict(base, state=state), {"microvmIdentifier": "microvm-test"})
                self.assertEqual(bridge.dispatch(request), expected)
            stub.add_client_error("get_microvm", "ResourceNotFoundException", expected_params={"microvmIdentifier": "microvm-test"})
            self.assertEqual(bridge.dispatch(request), "missing")
            stub.add_client_error("get_microvm", "AccessDeniedException", expected_params={"microvmIdentifier": "microvm-test"})
            with self.assertRaises(ClientError):
                bridge.dispatch(request)
            stub.add_response("get_microvm", dict(base, state="PENDING"), {"microvmIdentifier": "microvm-test"})
            with self.assertRaises(bridge.ProviderFailure):
                bridge.dispatch(request)
            stub.assert_no_pending_responses()

    def test_nitro_status_checks_job_ownership_and_ready_pod_uid(self):
        value = connection(); job, _ = job_fixture()
        value.request = Mock(side_effect=[job, {"items": [pod_fixture()]}])
        with patch.object(bridge, "NitroEks", return_value=value):
            self.assertEqual(bridge.dispatch({"operation": "sandbox_status", "sandbox": SANDBOX}), "running")
        self.assertTrue(all(call.args[0] == "GET" for call in value.request.call_args_list))
        value.request = Mock(return_value=None)
        with patch.object(bridge, "NitroEks", return_value=value):
            self.assertEqual(bridge.dispatch({"operation": "sandbox_status", "sandbox": SANDBOX}), "missing")
        job["metadata"]["uid"] = "99999999-9999-4999-8999-999999999999"
        value.request = Mock(return_value=job)
        with patch.object(bridge, "NitroEks", return_value=value), self.assertRaises(bridge.ProviderFailure):
            bridge.dispatch({"operation": "sandbox_status", "sandbox": SANDBOX})

    def test_nitro_status_distinguishes_terminal_jobs_from_transitions(self):
        for condition, expected in (("Complete", "stopped"), ("Failed", "stopped"),
                                    ("FailureTarget", None), ("SuccessCriteriaMet", None)):
            value = connection(); job, _ = job_fixture()
            job["status"] = {"conditions": [{"type": condition, "status": "True"}]}
            value.request = Mock(return_value=job)
            with self.subTest(condition=condition), patch.object(bridge, "NitroEks", return_value=value):
                if expected:
                    self.assertEqual(bridge.dispatch({"operation": "sandbox_status", "sandbox": SANDBOX}), expected)
                else:
                    with self.assertRaises(bridge.ProviderFailure):
                        bridge.dispatch({"operation": "sandbox_status", "sandbox": SANDBOX})
            value.request.assert_called_once()

    def test_pending_nitro_and_microvm_states_are_not_reported_as_stopped(self):
        value = connection(); job, _ = job_fixture()
        value.request = Mock(side_effect=[job, {"items": []}])
        with patch.object(bridge, "NitroEks", return_value=value), self.assertRaises(bridge.ProviderFailure):
            bridge.dispatch({"operation": "sandbox_status", "sandbox": SANDBOX})
        for state in ("PENDING", "SUSPENDING", "TERMINATING", "UNKNOWN"):
            aws = Mock(); aws.meta.region_name = "us-east-1"
            aws.get_microvm.return_value = {"microvmId": "microvm-test", "state": state}
            with self.subTest(state=state), patch.object(bridge, "client", return_value=aws), self.assertRaises(bridge.ProviderFailure):
                bridge.dispatch({"operation": "sandbox_status", "sandbox": {"provider_id": "microvm-test"}})

    def test_sagemaker_model_namespace_does_not_become_a_bedrock_selection(self):
        request = {"operation": "deployment_create", "spec": {"model": "bedrock/custom-model", "configuration": {}}}
        with patch.object(bridge, "manage", return_value="sagemaker") as manage, patch.object(bridge, "bedrock_selection") as selection:
            self.assertEqual(bridge.dispatch(request), "sagemaker")
            manage.assert_called_once_with(request)
            selection.assert_not_called()

    def test_invalid_bedrock_identifiers_fail_before_aws_clients(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(bridge, "client") as factory:
            for request in ({"operation": "deployment_create", "spec": {"model": "us.openai.bad model", "configuration": {}}},
                            {"operation": "deployment_status", "resource": {"provider_id": "bedrock/bad"}}):
                with self.assertRaises(bridge.ProviderFailure):
                    bridge.bedrock_selection(request)
            factory.assert_not_called()

    def test_setup_preserves_separate_resource_and_bedrock_regions(self):
        with patch.dict(os.environ, {"AWS_REGION": "us-east-2", "AWS_DEFAULT_REGION": "us-east-1"}, clear=True), patch.object(bridge, "CREDENTIAL", {}), patch.object(bridge, "caller_account", return_value=ACCOUNT):
            result = bridge.configure_connection({"options": {}})
            self.assertEqual(result["configuration"]["MAGICMTN_AWS_REGION"], "us-east-1")
            self.assertEqual(result["configuration"]["MAGICMTN_AWS_BEDROCK_REGION"], "us-east-2")
            self.assertEqual(bridge.bedrock_region(), "us-east-2")

    def test_nitro_namespace_replacement_is_rejected(self):
        value = connection()
        value.request = Mock(side_effect=[{"metadata": {"name": value.namespace, "uid": UID}},
                                          {"metadata": {"name": "isolated-sandbox", "uid": UID}}])
        with patch.dict(os.environ, {"MAGICMTN_AWS_NITRO_NAMESPACE_UID": "99999999-9999-4999-8999-999999999999"}, clear=True), patch.object(bridge, "NitroEks", return_value=value):
            with self.assertRaisesRegex(bridge.ProviderFailure, "identity changed"):
                bridge.validate_nitro_connection()


if __name__ == "__main__":
    unittest.main()
