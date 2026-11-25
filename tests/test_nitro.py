import base64
import copy
import io
import json
import os
from pathlib import Path
import ssl
import unittest
import urllib.error
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

from botocore.stub import Stubber
from test_bridge import api, bridge

SESSION = "cee98b1a-b843-40f1-97d2-1bfc96c9c676"
UID = "ea911877-bbea-4b23-98b2-d651c8de24cc"
JOB = "magicmtn-" + SESSION.replace("-", "")
SCOPE = "nitro-eks/us-east-1/123456789012/test-cluster/magicmtn-sandboxes"
IMAGE = "example/agent:1"
SANDBOX = {"provider_id": f"{SCOPE}/{JOB}/{UID}"}
RESULT = {"stdout": "literal;雪\n", "stderr": "", "exit_code": 0,
          "elapsed_ms": 1, "timed_out": False, "truncated": False}


def connection():
    value = bridge.NitroEks.__new__(bridge.NitroEks)
    value.namespace = "magicmtn-sandboxes"
    value.region = "us-east-1"
    value.scope = SCOPE
    value.endpoint = "https://example.gr7.us-east-1.eks.amazonaws.com"
    value.ca = "certificate"
    value.token = "secret-token"
    value.jobs = "/apis/batch/v1/namespaces/magicmtn-sandboxes/jobs"
    value.pods = "/api/v1/namespaces/magicmtn-sandboxes/pods"
    return value


def job_fixture():
    value = connection()
    saved = []

    def request(method, path, body=None, **kwargs):
        if method == "GET":
            return None
        job = copy.deepcopy(body)
        job["metadata"]["uid"] = UID
        saved.append(job)
        return job

    value.request = Mock(side_effect=request)
    result = value.create(IMAGE, SESSION, 60)
    return saved[0], result


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def patch(self):
        return patch.multiple(bridge.time, monotonic=self.monotonic, sleep=self.sleep)


def pod_fixture(status=None):
    return {"metadata": {"name": JOB + "-abcde", "namespace": "magicmtn-sandboxes",
                         "uid": "0f482a49-2881-47f9-bcdf-3e4f3f34d90c", "labels": bridge.NITRO_OWNER,
                         "ownerReferences": [{"kind": "Job", "uid": UID, "controller": True}]},
            "spec": {"runtimeClassName": "isolated-sandbox"},
            "status": {"phase": "Running", "containerStatuses": [{"name": "sandbox", "ready": True}]}
            if status is None else status}


def snapshot(*pods, version="1"):
    return {"metadata": {"resourceVersion": version}, "items": list(pods)}


class NitroTests(unittest.TestCase):
    def test_image_backend_is_opt_in_and_existing_ids_route_independently(self):
        for backend, template in (("nitro-eks", "image:"), ("microvm", "nitro-eks:")):
            request = {"operation": "sandbox_create", "template": template + IMAGE + "|" + SESSION}
            with patch.dict(os.environ, {"MAGICMTN_AWS_SANDBOX_BACKEND": backend}), patch.object(bridge, "nitro_dispatch", return_value=SANDBOX) as dispatch:
                self.assertEqual(bridge.dispatch(request), SANDBOX)
                dispatch.assert_called_once_with(request)
        with patch.dict(os.environ, {"MAGICMTN_AWS_SANDBOX_BACKEND": "microvm"}), patch.object(bridge, "nitro_dispatch") as dispatch:
            bridge.dispatch({"operation": "terminate", "sandbox": SANDBOX})
            dispatch.assert_called_once()

    def test_invalid_templates_fail_before_aws_and_ttl_is_capped(self):
        with patch.object(bridge, "NitroEks") as factory:
            for template in ("nitro-eks:image", "nitro-eks:image|bad", "nitro-eks:bad image|" + SESSION,
                             "nitro-eks:image|00000000-0000-0000-0000-000000000000"):
                with self.subTest(template=template), self.assertRaises(bridge.ProviderFailure):
                    bridge.dispatch({"operation": "sandbox_create", "template": template})
            for ttl in (True, 0, -1, "30"):
                with self.assertRaises(bridge.ProviderFailure):
                    bridge.dispatch({"operation": "sandbox_create", "template": "nitro-eks:" + IMAGE + "|" + SESSION, "ttl_seconds": ttl})
            factory.assert_not_called()
            bridge.dispatch({"operation": "sandbox_create", "template": "nitro-eks:" + IMAGE + "|" + SESSION, "ttl_seconds": 999999})
            factory.return_value.create.assert_called_once_with(IMAGE, SESSION, 28800)

    def test_job_enforces_nitro_isolation_and_expires_without_host_polling(self):
        job, result = job_fixture()
        self.assertEqual(result["provider_id"], SANDBOX["provider_id"])
        spec = job["spec"]
        self.assertEqual(spec["activeDeadlineSeconds"], 60)
        self.assertEqual(spec["ttlSecondsAfterFinished"], 300)
        self.assertEqual(spec["backoffLimit"], 0)
        pod = spec["template"]
        self.assertEqual(pod["metadata"]["annotations"]["sandbox.amazonaws.com/network-isolated"], "true")
        self.assertEqual(pod["spec"]["runtimeClassName"], "isolated-sandbox")
        self.assertFalse(pod["spec"]["automountServiceAccountToken"])
        self.assertEqual(pod["spec"]["restartPolicy"], "Never")
        self.assertEqual(pod["spec"]["containers"][0]["command"], ["python3", bridge.NITRO_AGENT, "--idle"])
        self.assertNotIn("token", json.dumps(result))

    def test_create_recovers_owned_job_without_replaying_creation(self):
        job, expected = job_fixture()
        value = connection()
        value.request = Mock(return_value=job)
        self.assertEqual(value.create(IMAGE, SESSION, 60), expected)
        value.request.assert_called_once_with("GET", value.jobs + "/" + JOB, allow_missing=True)
        for image, ttl in (("other/image:1", 60), (IMAGE, 61)):
            with self.assertRaises(bridge.ProviderFailure):
                value.create(image, SESSION, ttl)

    def test_create_never_replays_after_unknown_failure(self):
        value = connection()
        value.request = Mock(side_effect=[None, TimeoutError("upstream secret")])
        with self.assertRaises(TimeoutError):
            value.create(IMAGE, SESSION, 60)
        self.assertEqual([call.args[0] for call in value.request.call_args_list], ["GET", "POST"])

    def test_ownership_and_runtime_are_required(self):
        original, _ = job_fixture()
        value = connection()
        for change in (lambda j: j["metadata"].update(labels={}),
                       lambda j: j["metadata"].update(namespace="default"),
                       lambda j: j["metadata"].update(uid=SESSION),
                       lambda j: j["spec"]["template"]["spec"].update(runtimeClassName="runc")):
            job = copy.deepcopy(original)
            change(job)
            with self.assertRaises(bridge.ProviderFailure):
                value.owned_job(job, UID)

    def test_scope_is_in_id_and_cannot_be_overridden_by_metadata(self):
        value = connection()
        self.assertEqual(value.reference({**SANDBOX, "metadata": {"endpoint": "https://evil.test"}}), (JOB, UID))
        for old, new in (("us-east-1", "us-east-2"), ("123456789012", "111111111111"),
                         ("test-cluster", "other"), ("magicmtn-sandboxes", "default"),
                         (JOB, "../external"), (UID, "bad")):
            with self.assertRaises(bridge.ProviderFailure):
                value.reference({"provider_id": SANDBOX["provider_id"].replace(old, new)})

    def test_termination_pins_uid_and_cascades_to_pod(self):
        job, _ = job_fixture()
        value = connection()
        value.request = Mock(side_effect=[job, {}])
        with patch.object(bridge, "NitroEks", return_value=value):
            bridge.dispatch({"operation": "terminate", "sandbox": SANDBOX})
        delete = value.request.call_args
        self.assertEqual(delete.args[0], "DELETE")
        self.assertEqual(delete.args[2]["preconditions"], {"uid": UID})
        self.assertEqual(delete.args[2]["propagationPolicy"], "Foreground")
        value.request = Mock(return_value=None)
        with patch.object(bridge, "NitroEks", return_value=value):
            bridge.dispatch({"operation": "terminate", "sandbox": SANDBOX})
        self.assertEqual(value.request.call_count, 1)
        job["metadata"]["uid"] = SESSION
        value.request = Mock(return_value=job)
        with patch.object(bridge, "NitroEks", return_value=value), self.assertRaises(bridge.ProviderFailure):
            bridge.dispatch({"operation": "terminate", "sandbox": SANDBOX})
        self.assertEqual(value.request.call_count, 1)

    def test_suspend_resume_fail_before_network(self):
        with patch.object(bridge, "client") as client:
            for op in ("suspend", "resume"):
                with self.assertRaises(bridge.ProviderFailure) as error:
                    bridge.dispatch({"operation": op, "sandbox": SANDBOX})
                self.assertEqual(error.exception.code, "unsupported_operation")
            client.assert_not_called()

    def test_ready_pod_is_revalidated_and_submitted_once(self):
        job, _ = job_fixture()
        pod = pod_fixture()
        value = connection()
        value.request = Mock(side_effect=[job, snapshot(pod), job, pod])
        with patch.object(bridge, "nitro_exec", return_value=RESULT) as execute:
            self.assertEqual(value.execute(JOB, UID, {"command": "echo"}), RESULT)
            execute.assert_called_once_with(value, pod["metadata"]["name"], {"command": "echo"})
        self.assertTrue(all(c.args[0] == "GET" for c in value.request.call_args_list))
        self.assertEqual(value.request.call_args_list[-1].args[1], value.pods + "/" + pod["metadata"]["name"])

    def test_cold_pod_watch_and_expired_version_relist(self):
        for expired_event in (False, True):
            with self.subTest(expired_event=expired_event):
                job, _ = job_fixture()
                pod = pod_fixture()
                value = connection()
                clock = FakeClock()
                value.request = Mock(side_effect=lambda method, path, **kwargs: (
                    job if path.startswith(value.jobs) else pod if path.endswith(pod["metadata"]["name"])
                    else snapshot(version=str(clock.now))))
                value.opener = Mock()
                streams = []

                def watch(request, timeout):
                    params = parse_qs(urlsplit(request.full_url).query)
                    self.assertEqual(params["labelSelector"], ["batch.kubernetes.io/controller-uid=" + UID])
                    self.assertEqual(params["watch"], ["true"])
                    self.assertEqual(params["resourceVersion"], [str(clock.now)])
                    if not streams:
                        if not expired_event:
                            streams.append(io.BytesIO())
                            raise urllib.error.HTTPError(request.full_url, 410, "secret", {}, streams[-1])
                        stream = io.BytesIO(b'{"type":"ERROR","object":{"code":410}}\n')
                    elif clock.now < 30:
                        clock.sleep(min(4, 30 - clock.now))
                        stream = io.BytesIO()
                    else:
                        stream = io.BytesIO((json.dumps({"type": "MODIFIED", "object": pod}) + "\n").encode())
                    streams.append(stream)
                    return stream

                value.opener.open.side_effect = watch
                with clock.patch(), patch.object(bridge, "nitro_exec", return_value=RESULT) as execute:
                    self.assertEqual(value.execute(JOB, UID, {}), RESULT)
                    execute.assert_called_once_with(value, pod["metadata"]["name"], {})
                self.assertGreaterEqual(clock.now, 30)
                self.assertLess(clock.now, 45)
                self.assertTrue(all(stream.closed for stream in streams))

    def test_watch_forbidden_falls_back_to_polling(self):
        job, _ = job_fixture()
        pod = pod_fixture()
        value = connection()
        clock = FakeClock()
        value.request = Mock(side_effect=lambda method, path, **kwargs: (
            job if path.startswith(value.jobs) else pod if path.endswith(pod["metadata"]["name"])
            else snapshot(pod) if clock.now >= 30 else snapshot()))
        value.opener = Mock()
        response = io.BytesIO(b"secret")
        value.opener.open.side_effect = urllib.error.HTTPError(value.endpoint, 403, "secret", {}, response)
        with clock.patch(), patch.object(bridge, "nitro_exec", return_value=RESULT) as execute:
            self.assertEqual(value.execute(JOB, UID, {}), RESULT)
            execute.assert_called_once()
        self.assertTrue(response.closed)
        value.opener.open.assert_called_once()
        self.assertGreaterEqual(clock.now, 30)
        self.assertLess(clock.now, 45)

    def test_startup_deadline_reports_last_blocker(self):
        job, _ = job_fixture()
        for status, message in (
                ({"conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": "secret"}]},
                 "Nitro sandbox is waiting for node capacity or scheduling constraints"),
                ({"containerStatuses": [{"name": "sandbox", "ready": False, "state": {"waiting": {"reason": "ImagePullBackOff", "message": "secret"}}}]},
                 "Nitro sandbox image could not be pulled"),
                ({}, "Nitro sandbox is not ready")):
            with self.subTest(message=message):
                pod = pod_fixture(status)
                clock = FakeClock()
                value = connection()
                value.request = Mock(side_effect=lambda method, path, **kwargs: job if path.startswith(value.jobs) else snapshot(pod))
                value.opener = Mock()
                value.opener.open.side_effect = lambda *args, **kwargs: io.BytesIO()
                with clock.patch(), patch.object(bridge, "nitro_exec") as execute, self.assertRaises(bridge.ProviderFailure) as error:
                    value.execute(JOB, UID, {})
                self.assertEqual(error.exception.message, message)
                self.assertEqual(clock.now, 45)
                execute.assert_not_called()

    def test_terminal_job_and_identity_errors_do_not_submit(self):
        original, _ = job_fixture()
        for change, message in (
                (lambda j: j.update(status={"conditions": [{"status": "True", "type": "FailureTarget", "reason": "DeadlineExceeded"}]}), "Nitro sandbox lifetime expired"),
                (lambda j: j.update(status={"conditions": [{"status": "True", "type": "SuccessCriteriaMet"}]}), "Nitro sandbox has finished"),
                (lambda j: j["metadata"].update(deletionTimestamp="now"), "Nitro sandbox is terminating"),
                (lambda j: j["metadata"].update(uid=SESSION), "Nitro Job identity changed")):
            job = copy.deepcopy(original)
            change(job)
            value = connection()
            value.request = Mock(return_value=job)
            with patch.object(bridge, "nitro_exec") as execute, self.assertRaises(bridge.ProviderFailure) as error:
                value.execute(JOB, UID, {})
            self.assertEqual(error.exception.message, message)
            execute.assert_not_called()
            value.request.assert_called_once()
        pod = pod_fixture()
        pod["metadata"]["ownerReferences"][0]["uid"] = SESSION
        value = connection()
        value.request = Mock(side_effect=[original, snapshot(pod)])
        with patch.object(bridge, "nitro_exec") as execute, self.assertRaisesRegex(bridge.ProviderFailure, "ownership or runtime"):
            value.execute(JOB, UID, {})
        execute.assert_not_called()

    def test_disappearing_candidate_reobserved_and_exec_never_replayed(self):
        job, _ = job_fixture()
        pod = pod_fixture()
        value = connection()
        value.request = Mock(side_effect=[job, snapshot(pod), job, None, job, snapshot(pod), job, pod])
        value.opener = Mock()
        value.opener.open.return_value = io.BytesIO()
        clock = FakeClock()
        with clock.patch(), patch.object(bridge, "nitro_exec", side_effect=TimeoutError("transport failed")) as execute:
            with self.assertRaises(TimeoutError):
                value.execute(JOB, UID, {})
            execute.assert_called_once()
        self.assertTrue(value.opener.open.return_value.closed)

    def test_revalidation_rejects_replaced_job_and_pod(self):
        job, _ = job_fixture()
        replacement_job = copy.deepcopy(job)
        replacement_job["metadata"]["uid"] = SESSION
        pod = pod_fixture()
        value = connection()
        value.request = Mock(side_effect=[job, snapshot(pod), replacement_job])
        with patch.object(bridge, "nitro_exec") as execute, self.assertRaisesRegex(bridge.ProviderFailure, "Job identity changed"):
            value.execute(JOB, UID, {})
        execute.assert_not_called()
        replacement_pod = copy.deepcopy(pod)
        replacement_pod["metadata"]["uid"] = SESSION
        value.request = Mock(side_effect=[job, snapshot(pod), job, replacement_pod, None])
        value.opener = Mock()
        value.opener.open.return_value = io.BytesIO()
        clock = FakeClock()
        with clock.patch(), patch.object(bridge, "nitro_exec") as execute, self.assertRaisesRegex(bridge.ProviderFailure, "sandbox was removed"):
            value.execute(JOB, UID, {})
        execute.assert_not_called()

    def test_discovery_consumes_the_same_bounded_readiness_budget(self):
        def discover(deadline):
            self.assertGreater(deadline, bridge.time.monotonic())
            bridge.time.sleep(5)

        start = bridge.time.monotonic()
        with patch.object(bridge, "NITRO_STARTUP_SECONDS", 0.1), patch.object(bridge, "NitroEks", side_effect=discover), patch.object(bridge, "nitro_exec") as execute, self.assertRaisesRegex(bridge.ProviderFailure, "sandbox is not ready"):
            bridge.dispatch({"operation": "exec", "sandbox": SANDBOX})
        self.assertLess(bridge.time.monotonic() - start, 1)
        execute.assert_not_called()

    def test_stalled_watch_is_interrupted_and_closed(self):
        job, _ = job_fixture()
        value = connection()
        value.request = Mock(side_effect=lambda method, path, **kwargs: job if path.startswith(value.jobs) else snapshot())
        value.opener = Mock()

        class Stalled(io.BytesIO):
            def readline(self, *args):
                bridge.time.sleep(5)
                return b""

        stream = Stalled()
        value.opener.open.return_value = stream
        start = bridge.time.monotonic()
        with patch.object(bridge, "nitro_exec") as execute, self.assertRaises(bridge.ProviderFailure) as error:
            value.execute(JOB, UID, {}, start + 0.1)
        self.assertEqual(error.exception.message, "Nitro sandbox is not ready")
        self.assertLess(bridge.time.monotonic() - start, 1)
        self.assertTrue(stream.closed)
        execute.assert_not_called()

    def test_control_path_forbidden_is_not_watch_fallback(self):
        value = connection()
        value.opener = Mock()
        body = io.BytesIO(b"secret")
        value.opener.open.side_effect = urllib.error.HTTPError(value.endpoint, 403, "secret", {}, body)
        with patch.object(bridge, "nitro_exec") as execute, self.assertRaises(bridge.ProviderFailure) as error:
            value.execute(JOB, UID, {})
        self.assertEqual(error.exception.message, "Nitro EKS readiness read was not authorized")
        self.assertTrue(body.closed)
        execute.assert_not_called()
        value.opener.open.assert_called_once()

    def test_sts_token_signature_is_bound_to_cluster(self):
        sts = api("sts")
        with patch.object(bridge, "client", return_value=sts):
            token = bridge.nitro_token("test-cluster")
        encoded = token.removeprefix("k8s-aws-v1.")
        signed = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        query = parse_qs(urlsplit(signed).query)
        self.assertIn("x-k8s-aws-id", query["X-Amz-SignedHeaders"][0])
        self.assertEqual(query["X-Amz-Expires"], ["60"])
        self.assertEqual(query["Action"], ["GetCallerIdentity"])

    def test_cluster_connection_uses_aws_endpoint_and_ca(self):
        eks = api("eks")
        info = {"name": "test-cluster", "arn": "arn:aws:eks:us-east-1:123456789012:cluster/test-cluster",
                "status": "ACTIVE", "endpoint": "https://example.gr7.us-east-1.eks.amazonaws.com",
                "certificateAuthority": {"data": base64.b64encode(b"test-ca").decode()}}
        with Stubber(eks) as stub, patch.dict(os.environ, {"MAGICMTN_AWS_NITRO_EKS_CLUSTER": "test-cluster"}), patch.object(bridge, "client", return_value=eks), patch.object(bridge, "nitro_token", return_value="secret"), patch.object(ssl, "create_default_context") as tls, patch.object(bridge.urllib.request, "build_opener"):
            stub.add_response("describe_cluster", {"cluster": info}, {"name": "test-cluster"})
            value = bridge.NitroEks()
            self.assertEqual(value.scope, SCOPE)
            tls.assert_called_once_with(cadata="test-ca")
            for endpoint in ("http://example.gr7.us-east-1.eks.amazonaws.com", "https://evil.test",
                             "https://example.us-east-1.eks.amazonaws.com@evil.test",
                             "https://example.us-east-1.eks.amazonaws.com/path",
                             "https://example.us-east-2.eks.amazonaws.com"):
                info["endpoint"] = endpoint
                stub.add_response("describe_cluster", {"cluster": copy.deepcopy(info)}, {"name": "test-cluster"})
                with self.assertRaises(bridge.ProviderFailure):
                    bridge.NitroEks()
            stub.assert_no_pending_responses()

    def test_api_errors_do_not_disclose_bodies_or_retry(self):
        value = connection()
        value.opener = Mock()
        for code in (301, 403, 409, 500):
            body = io.BytesIO(b"secret upstream response")
            value.opener.open.side_effect = urllib.error.HTTPError(value.endpoint, code, "secret", {}, body)
            with self.assertRaises(bridge.ProviderFailure) as error:
                value.request("POST", value.jobs, {})
            self.assertNotIn("secret", error.exception.message)
            self.assertTrue(body.closed)
        self.assertEqual(value.opener.open.call_count, 4)

    def test_exec_transport_uses_private_pipe_and_preserves_literal_argv(self):
        fixture = str(Path(__file__).parent / "fixtures/fake-kubectl.py")
        request = {"command": "echo", "args": ["literal;雪"], "timeout_ms": 1000, "max_output_bytes": 1024}
        with patch.dict(os.environ, {"MAGICMTN_AWS_KUBECTL": fixture}):
            result = bridge.nitro_exec(connection(), JOB + "-abcde", request)
        self.assertEqual(result["stdout"], "literal;雪\n")
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["timed_out"])

    def test_exec_transport_bounds_output_and_rejects_nonzero_exit(self):
        fixture = str(Path(__file__).parent / "fixtures/fake-kubectl.py")
        for command in ("oversized", "fail"):
            with patch.dict(os.environ, {"MAGICMTN_AWS_KUBECTL": fixture}), self.assertRaises(bridge.ProviderFailure):
                bridge.nitro_exec(connection(), JOB + "-abcde", {
                    "command": command, "args": [], "timeout_ms": 1000, "max_output_bytes": 1024})


if __name__ == "__main__":
    unittest.main()
