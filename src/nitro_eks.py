"""Nitro Sandbox Jobs on EKS. Prepended to aws_bridge.py in the executable."""
import base64
import selectors
import ssl
import subprocess
from urllib.parse import urlencode

NITRO_OWNER = {"magicmtn.ai/provider": "aws-nitro-v1"}
NITRO_AGENT = "/opt/magicmtn-agent.py"


def nitro_matches(expected, actual):
    """Compare requested fields while allowing API-server defaults."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and nitro_matches(value, actual[key]) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(
            nitro_matches(left, right) for left, right in zip(expected, actual))
    return type(expected) is type(actual) and expected == actual


def nitro_namespace(value):
    require(isinstance(value, str) and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value),
            "invalid Nitro EKS namespace")
    return value


def nitro_session(value):
    try:
        result = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise ProviderFailure("invalid Nitro session UUID") from None
    require(result.int != 0, "invalid Nitro session UUID")
    return str(result)


def nitro_token(cluster):
    # EKS authenticates a presigned STS identity request bound to this cluster.
    sts = client("sts")

    def add_cluster_header(request, **kwargs):
        request.headers["x-k8s-aws-id"] = cluster

    sts.meta.events.register("before-sign.sts.GetCallerIdentity", add_cluster_header)
    signed = sts.generate_presigned_url("get_caller_identity", ExpiresIn=60, HttpMethod="GET")
    return "k8s-aws-v1." + base64.urlsafe_b64encode(signed.encode()).decode().rstrip("=")


class NitroEks:
    def __init__(self):
        cluster = os.environ.get("MAGICMTN_AWS_NITRO_EKS_CLUSTER", "")
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", cluster),
                "set MAGICMTN_AWS_NITRO_EKS_CLUSTER to the EKS cluster name")
        self.namespace = nitro_namespace(os.environ.get("MAGICMTN_AWS_NITRO_EKS_NAMESPACE", "magicmtn-sandboxes"))
        api = client("eks")
        self.region = api.meta.region_name
        info = api.describe_cluster(name=cluster)["cluster"]
        arn = info.get("arn", "").split(":", 5)
        require(len(arn) == 6 and arn[:3] == ["arn", api.meta.partition, "eks"]
                and arn[3] == self.region and re.fullmatch(r"[0-9]{12}", arn[4])
                and arn[5] == "cluster/" + cluster, "unexpected EKS cluster identity")
        require(info.get("status") == "ACTIVE", "Nitro EKS cluster must be active")
        self.scope = f"nitro-eks/{self.region}/{arn[4]}/{cluster}/{self.namespace}"
        self.endpoint = info["endpoint"]
        parsed = urlsplit(self.endpoint)
        host = parsed.hostname or ""
        suffixes = (f".{self.region}.eks.amazonaws.com", f".{self.region}.api.aws")
        if api.meta.partition == "aws-cn":
            suffixes = (f".{self.region}.amazonwebservices.com.cn", f".{self.region}.api.amazonwebservices.com.cn")
        require(parsed.scheme == "https" and host.endswith(suffixes)
                and parsed.port is None and not parsed.username and not parsed.password
                and parsed.path in ("", "/") and not parsed.query and not parsed.fragment,
                "unexpected EKS cluster endpoint")
        self.endpoint = self.endpoint.rstrip("/")
        self.ca = info["certificateAuthority"]["data"]
        context = ssl.create_default_context(cadata=base64.b64decode(self.ca, validate=True).decode())
        self.token = nitro_token(cluster)
        self.opener = urllib.request.build_opener(
            NoRedirect(), urllib.request.HTTPSHandler(context=context))
        self.jobs = f"/apis/batch/v1/namespaces/{self.namespace}/jobs"
        self.pods = f"/api/v1/namespaces/{self.namespace}/pods"

    def request(self, method, path, body=None, allow_missing=False):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.endpoint + path, data=data, method=method,
                                     headers={"Authorization": "Bearer " + self.token,
                                              "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=10) as response:
                raw = response.read(LIMIT + 1)
        except urllib.error.HTTPError as error:
            error.close()
            if allow_missing and error.code == 404:
                return None
            raise ProviderFailure("Nitro EKS API request failed; reconcile resource state before retrying",
                                  "upstream_unavailable") from None
        require(len(raw) <= LIMIT, "Nitro EKS response too large")
        return json.loads(raw)

    def owned_job(self, job, expected_uid=None):
        metadata = job.get("metadata", {})
        require(metadata.get("labels", {}).get("magicmtn.ai/provider") == NITRO_OWNER["magicmtn.ai/provider"],
                "Nitro Job is not managed by this provider")
        require(metadata.get("namespace") == self.namespace, "Nitro Job namespace mismatch")
        uid = nitro_session(metadata.get("uid"))
        require(expected_uid is None or uid == expected_uid, "Nitro Job identity changed")
        require(job.get("spec", {}).get("template", {}).get("spec", {}).get("runtimeClassName") == "isolated-sandbox",
                "Nitro Job runtime class changed")
        return uid

    def reference(self, sandbox):
        parts = sandbox["provider_id"].split("/")
        require(len(parts) == 7 and "/".join(parts[:5]) == self.scope,
                "Nitro sandbox belongs to a different AWS account, region, cluster, or namespace")
        require(re.fullmatch(r"magicmtn-[0-9a-f]{32}", parts[5]), "invalid Nitro Job name")
        return parts[5], nitro_session(parts[6])

    def create(self, image, session, ttl):
        job_name = "magicmtn-" + uuid.UUID(session).hex
        labels = {**NITRO_OWNER, "magicmtn.ai/session": session}
        pod = {"metadata": {"labels": labels,
                            "annotations": {"sandbox.amazonaws.com/network-isolated": "true"}},
               "spec": {"runtimeClassName": "isolated-sandbox", "restartPolicy": "Never",
                        "automountServiceAccountToken": False,
                        "securityContext": {"runAsNonRoot": True, "runAsUser": 1000,
                                            "seccompProfile": {"type": "RuntimeDefault"}},
                        "containers": [{"name": "sandbox", "image": image,
                                        "command": ["python3", NITRO_AGENT, "--idle"],
                                        "resources": {"requests": {"cpu": "1", "memory": "512Mi"},
                                                      "limits": {"cpu": "1", "memory": "512Mi"}},
                                        "securityContext": {"allowPrivilegeEscalation": False,
                                                            "capabilities": {"drop": ["ALL"]}}}]}}
        spec = {"backoffLimit": 0, "activeDeadlineSeconds": ttl,
                "ttlSecondsAfterFinished": 300, "template": pod}
        job = self.request("GET", self.jobs + "/" + job_name, allow_missing=True)
        if job is None:
            job = self.request("POST", self.jobs, {"apiVersion": "batch/v1", "kind": "Job",
                               "metadata": {"name": job_name, "namespace": self.namespace,
                                            "labels": labels}, "spec": spec})
        uid = self.owned_job(job)
        require(job["metadata"].get("name") == job_name
                and job["metadata"].get("labels", {}).get("magicmtn.ai/session") == session
                and nitro_matches(spec, job["spec"]),
                "Nitro session already exists with different configuration")
        conditions = job.get("status", {}).get("conditions", [])
        finished = any(c.get("status") == "True" and c.get("type") in ("Complete", "Failed") for c in conditions)
        require(not finished and not job["metadata"].get("deletionTimestamp"), "Nitro session has finished")
        return {"provider_id": f"{self.scope}/{job_name}/{uid}", "status": "PENDING",
                "create_time_ms": None, "metadata": {"backend": "nitro-eks", "region": self.region,
                "image": image, "session": session, "runtime_class": "isolated-sandbox"}}

    def execute(self, job_name, uid, request):
        deadline = time.monotonic() + 15
        while True:
            job = self.request("GET", self.jobs + "/" + job_name)
            self.owned_job(job, uid)
            require(not job["metadata"].get("deletionTimestamp"), "Nitro sandbox is terminating")
            require(not any(c.get("status") == "True" and c.get("type") in ("Complete", "Failed")
                            for c in job.get("status", {}).get("conditions", [])), "Nitro sandbox has finished")
            query = urlencode({"labelSelector": "batch.kubernetes.io/controller-uid=" + uid, "limit": 10})
            pods = self.request("GET", self.pods + "?" + query).get("items", [])
            for pod in pods:
                metadata = pod.get("metadata", {})
                owner = any(o.get("uid") == uid and o.get("kind") == "Job" and o.get("controller") is True
                            for o in metadata.get("ownerReferences", []))
                require(owner and metadata.get("namespace") == self.namespace
                        and metadata.get("labels", {}).get("magicmtn.ai/provider") == NITRO_OWNER["magicmtn.ai/provider"]
                        and pod.get("spec", {}).get("runtimeClassName") == "isolated-sandbox",
                        "unexpected Nitro sandbox pod ownership or runtime")
                if pod.get("status", {}).get("phase") == "Running" and not metadata.get("deletionTimestamp"):
                    ready = any(c.get("name") == "sandbox" and c.get("ready") is True
                                for c in pod.get("status", {}).get("containerStatuses", []))
                    if ready:
                        pod_name = metadata["name"]
                        require(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,252}", pod_name), "invalid Nitro pod name")
                        return nitro_exec(self, pod_name, request)
            if time.monotonic() >= deadline:
                raise ProviderFailure("Nitro sandbox is not ready", "upstream_unavailable")
            time.sleep(0.2)


def nitro_exec(api, pod, request):
    body = {k: request[k] for k in ("command", "args", "timeout_ms", "max_output_bytes")}
    config = {"apiVersion": "v1", "kind": "Config", "current-context": "nitro",
              "clusters": [{"name": "nitro", "cluster": {"server": api.endpoint, "certificate-authority-data": api.ca}}],
              "users": [{"name": "nitro", "user": {"token": api.token}}],
              "contexts": [{"name": "nitro", "context": {"cluster": "nitro", "user": "nitro", "namespace": api.namespace}}]}
    # Keep the short-lived token off disk and out of argv. Both inputs use pipes.
    config_read, config_write = os.pipe()
    try:
        command = [os.environ.get("MAGICMTN_AWS_KUBECTL", "kubectl"),
                   "--kubeconfig", f"/dev/fd/{config_read}", "--request-timeout=40s",
                   "exec", pod, "--container=sandbox", "--stdin", "--",
                   "python3", NITRO_AGENT, "--stdio"]
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, pass_fds=(config_read,)) as process:
            try:
                raw = bytearray()
                deadline = time.monotonic() + 40
                with selectors.DefaultSelector() as selector:
                    for fd, data in ((config_write, json.dumps(config).encode()),
                                     (process.stdin.fileno(), json.dumps(body).encode())):
                        os.set_blocking(fd, False)
                        selector.register(fd, selectors.EVENT_WRITE, memoryview(data))
                    os.set_blocking(process.stdout.fileno(), False)
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while selector.get_map():
                        if time.monotonic() >= deadline:
                            raise ProviderFailure("Nitro exec timed out; reconcile before retrying", "upstream_unavailable")
                        for key, _ in selector.select(0.1):
                            if key.events == selectors.EVENT_WRITE:
                                remaining = key.data[os.write(key.fd, key.data):]
                                if remaining:
                                    selector.modify(key.fd, selectors.EVENT_WRITE, remaining)
                                else:
                                    selector.unregister(key.fd)
                                    if key.fd == config_write:
                                        os.close(config_write)
                                        config_write = None
                                    else:
                                        process.stdin.close()
                            else:
                                chunk = os.read(key.fd, 8192)
                                if not chunk:
                                    selector.unregister(key.fileobj)
                                raw.extend(chunk)
                                require(len(raw) <= 128 * 1024, "Nitro exec response too large")
                    require(process.wait(timeout=max(0.01, deadline - time.monotonic())) == 0,
                            "Nitro exec failed; reconcile before retrying")
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
    finally:
        os.close(config_read)
        if config_write is not None:
            os.close(config_write)
    result = json.loads(raw)
    require(isinstance(result, dict) and isinstance(result.get("stdout"), str)
            and isinstance(result.get("stderr"), str)
            and len(result["stdout"].encode()) + len(result["stderr"].encode()) <= body["max_output_bytes"]
            and type(result.get("timed_out")) is bool and type(result.get("truncated")) is bool
            and type(result.get("elapsed_ms")) is int and result["elapsed_ms"] >= 0
            and (result.get("exit_code") is None or type(result["exit_code"]) is int),
            "invalid Nitro exec response")
    return result


def nitro_dispatch(request):
    op = request["operation"]
    if op in ("suspend", "resume"):
        raise ProviderFailure("Nitro EKS supports create, exec, and terminate", "unsupported_operation")
    if op == "sandbox_create":
        parts = request["template"].split(":", 1)[1].rsplit("|", 1)
        require(len(parts) == 2, "Nitro template requires an OCI image and session UUID")
        image, session = parts
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/_.:@-]{0,511}", image), "invalid Nitro OCI image")
        session = nitro_session(session)
        ttl = request.get("ttl_seconds", 28800)
        require(type(ttl) is int and ttl > 0, "invalid Nitro sandbox TTL")
        return NitroEks().create(image, session, min(ttl, 28800))
    api = NitroEks()
    job_name, uid = api.reference(request["sandbox"])
    if op == "terminate":
        job = api.request("GET", api.jobs + "/" + job_name, allow_missing=True)
        if job is not None:
            api.owned_job(job, uid)
            api.request("DELETE", api.jobs + "/" + job_name,
                        {"apiVersion": "v1", "kind": "DeleteOptions", "propagationPolicy": "Foreground",
                         "preconditions": {"uid": uid}}, allow_missing=True)
        return None
    if op == "exec":
        return api.execute(job_name, uid, request)
    raise ProviderFailure("unsupported Nitro operation", "unsupported_operation")
