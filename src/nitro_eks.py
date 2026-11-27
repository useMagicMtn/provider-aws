"""Nitro Sandbox Jobs on EKS. Prepended to aws_bridge.py in the executable."""
import base64
import hashlib
import selectors
import signal
import ssl
import subprocess
from contextlib import closing, contextmanager
from urllib.parse import urlencode

NITRO_OWNER = {"magicmtn.ai/provider": "aws-nitro-v1"}
NITRO_AGENT = "/opt/magicmtn-agent.py"
NITRO_STARTUP_SECONDS = 45
NITRO_NOT_READY = "Nitro sandbox is not ready"


class NitroWaitExpired(Exception):
    pass


class NitroWatchForbidden(Exception):
    pass


@contextmanager
def nitro_deadline(deadline):
    """Bound wall time across discovery, headers and partial stream reads.

    The embedded adapter runs one operation on its main thread (POSIX).
    Nested watch windows restore the enclosing absolute startup deadline.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise NitroWaitExpired()
    previous = signal.getsignal(signal.SIGALRM)
    started = time.monotonic()

    def expire(signum, frame):
        raise NitroWaitExpired()

    signal.signal(signal.SIGALRM, expire)
    old_timer = signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if old_timer[0]:
            signal.setitimer(signal.ITIMER_REAL,
                             max(0.000001, old_timer[0] - (time.monotonic() - started)))


def nitro_remaining(deadline, maximum):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise NitroWaitExpired()
    return min(maximum, remaining)


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


def nitro_token(cluster, deadline=None):
    # EKS authenticates a presigned STS identity request bound to this cluster.
    sts = client("sts") if deadline is None else client(
        "sts", connect_timeout=nitro_remaining(deadline, 5), read_timeout=nitro_remaining(deadline, 5))

    def add_cluster_header(request, **kwargs):
        request.headers["x-k8s-aws-id"] = cluster

    sts.meta.events.register("before-sign.sts.GetCallerIdentity", add_cluster_header)
    signed = sts.generate_presigned_url("get_caller_identity", ExpiresIn=60, HttpMethod="GET")
    return "k8s-aws-v1." + base64.urlsafe_b64encode(signed.encode()).decode().rstrip("=")


class NitroEks:
    def __init__(self, deadline=None):
        cluster = os.environ.get("MAGICMTN_AWS_NITRO_EKS_CLUSTER", "")
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", cluster),
                "set MAGICMTN_AWS_NITRO_EKS_CLUSTER to the EKS cluster name")
        self.namespace = nitro_namespace(os.environ.get("MAGICMTN_AWS_NITRO_EKS_NAMESPACE", "magicmtn-sandboxes"))
        api = client("eks") if deadline is None else client(
            "eks", connect_timeout=nitro_remaining(deadline, 5), read_timeout=nitro_remaining(deadline, 10))
        self.region = api.meta.region_name
        info = api.describe_cluster(name=cluster)["cluster"]
        arn = info.get("arn", "").split(":", 5)
        require(len(arn) == 6 and arn[:3] == ["arn", api.meta.partition, "eks"]
                and arn[3] == self.region and re.fullmatch(r"[0-9]{12}", arn[4])
                and arn[5] == "cluster/" + cluster, "unexpected EKS cluster identity")
        require(info.get("status") == "ACTIVE", "Nitro EKS cluster must be active")
        self.cluster_arn = info["arn"]
        expected = os.environ.get("MAGICMTN_AWS_NITRO_EKS_CLUSTER_ARN")
        require(not expected or expected == self.cluster_arn, "Nitro EKS cluster identity changed")
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
        certificate = base64.b64decode(self.ca, validate=True)
        self.ca_sha256 = hashlib.sha256(certificate).hexdigest()
        expected_ca = os.environ.get("MAGICMTN_AWS_NITRO_CA_SHA256")
        require(not expected_ca or expected_ca == self.ca_sha256, "Nitro EKS cluster certificate identity changed")
        context = ssl.create_default_context(cadata=certificate.decode())
        self.token = nitro_token(cluster) if deadline is None else nitro_token(cluster, deadline)
        self.opener = urllib.request.build_opener(
            NoRedirect(), urllib.request.HTTPSHandler(context=context))
        self.jobs = f"/apis/batch/v1/namespaces/{self.namespace}/jobs"
        self.pods = f"/api/v1/namespaces/{self.namespace}/pods"

    def request(self, method, path, body=None, allow_missing=False, deadline=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.endpoint + path, data=data, method=method,
                                     headers={"Authorization": "Bearer " + self.token,
                                              "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=10 if deadline is None else nitro_remaining(deadline, 10)) as response:
                raw = response.read(LIMIT + 1)
        except urllib.error.HTTPError as error:
            error.close()
            if allow_missing and error.code == 404:
                return None
            if deadline is not None:
                message = ("Nitro EKS readiness read was not authorized" if error.code in (401, 403)
                           else "Nitro EKS readiness read failed")
                raise ProviderFailure(message, "upstream_unavailable") from None
            raise ProviderFailure("Nitro EKS API request failed; reconcile resource state before retrying",
                                  "upstream_unavailable") from None
        require(len(raw) <= LIMIT, "Nitro EKS response too large")
        try:
            result = json.loads(raw)
            require(isinstance(result, dict), "invalid Nitro EKS response")
            return result
        except (ValueError, UnicodeError):
            raise ProviderFailure("invalid Nitro EKS response", "upstream_unavailable") from None

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

    def active_job(self, job_name, uid, deadline):
        job = self.request("GET", self.jobs + "/" + job_name, allow_missing=True, deadline=deadline)
        require(job is not None, "Nitro sandbox was removed")
        self.owned_job(job, uid)
        require(not job["metadata"].get("deletionTimestamp"), "Nitro sandbox is terminating")
        for condition in job.get("status", {}).get("conditions", []):
            if condition.get("status") == "True" and condition.get("type") in (
                    "FailureTarget", "Failed", "SuccessCriteriaMet", "Complete"):
                if condition.get("reason") == "DeadlineExceeded":
                    raise ProviderFailure("Nitro sandbox lifetime expired")
                raise ProviderFailure("Nitro sandbox has finished")

    def pod_observation(self, pod, uid):
        metadata = pod.get("metadata", {})
        owner = any(o.get("uid") == uid and o.get("kind") == "Job" and o.get("controller") is True
                    for o in metadata.get("ownerReferences", []))
        require(owner and metadata.get("namespace") == self.namespace
                and metadata.get("labels", {}).get("magicmtn.ai/provider") == NITRO_OWNER["magicmtn.ai/provider"]
                and pod.get("spec", {}).get("runtimeClassName") == "isolated-sandbox",
                "unexpected Nitro sandbox pod ownership or runtime")
        pod_name = metadata.get("name", "")
        require(isinstance(pod_name, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,252}", pod_name),
                "invalid Nitro pod name")
        pod_uid = nitro_session(metadata.get("uid"))
        if metadata.get("deletionTimestamp"):
            return None, None
        status = pod.get("status", {})
        for container in status.get("containerStatuses", []):
            if container.get("name") != "sandbox":
                continue
            if status.get("phase") == "Running" and container.get("ready") is True:
                return (pod_name, pod_uid), None
            if container.get("state", {}).get("waiting", {}).get("reason") in ("ErrImagePull", "ImagePullBackOff"):
                return None, "Nitro sandbox image could not be pulled"
        for condition in status.get("conditions", []):
            if (condition.get("type"), condition.get("status"), condition.get("reason")) == (
                    "PodScheduled", "False", "Unschedulable"):
                return None, "Nitro sandbox is waiting for node capacity or scheduling constraints"
        return None, None

    def revalidate(self, candidate, job_name, uid, deadline):
        self.active_job(job_name, uid, deadline)
        pod = self.request("GET", self.pods + "/" + candidate[0], allow_missing=True, deadline=deadline)
        if pod is None:
            return False, None
        actual, reason = self.pod_observation(pod, uid)
        return actual == candidate, reason

    def watch_pods(self, selector, version, deadline):
        window = min(deadline, time.monotonic() + 5)
        seconds = max(1, int(window - time.monotonic()))
        query = urlencode({"labelSelector": selector, "watch": "true", "resourceVersion": version,
                           "timeoutSeconds": seconds})
        request = urllib.request.Request(self.endpoint + self.pods + "?" + query,
                                         headers={"Authorization": "Bearer " + self.token,
                                                  "Accept": "application/json"})
        try:
            with nitro_deadline(window):
                with self.opener.open(request, timeout=nitro_remaining(window, 5)) as response:
                    while True:
                        line = response.readline(LIMIT + 1)
                        if not line:
                            return
                        require(len(line) <= LIMIT, "Nitro EKS watch event too large")
                        try:
                            event = json.loads(line)
                        except (ValueError, UnicodeError):
                            raise ProviderFailure("invalid Nitro EKS watch event", "upstream_unavailable") from None
                        require(isinstance(event, dict) and isinstance(event.get("object"), dict),
                                "invalid Nitro EKS watch event")
                        kind, obj = event.get("type"), event["object"]
                        if kind == "ERROR" and obj.get("code") == 410:
                            return
                        if kind not in ("ADDED", "MODIFIED", "DELETED", "BOOKMARK"):
                            raise ProviderFailure("Nitro EKS readiness watch failed", "upstream_unavailable")
                        if kind in ("ADDED", "MODIFIED"):
                            yield obj
        except urllib.error.HTTPError as error:
            error.close()
            if error.code == 410:
                return
            if error.code == 403:
                raise NitroWatchForbidden() from None
            raise ProviderFailure("Nitro EKS readiness watch failed", "upstream_unavailable") from None
        except (NitroWaitExpired, TimeoutError):
            if time.monotonic() >= deadline:
                raise NitroWaitExpired() from None
            # Recheck the Job and relist after each short watch window.
            return
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                return
            raise ProviderFailure("Nitro EKS readiness watch failed", "upstream_unavailable") from None

    def wait_ready(self, job_name, uid, deadline):
        blocker = NITRO_NOT_READY
        polling = False
        backoff = 0.2
        selector = "batch.kubernetes.io/controller-uid=" + uid
        query = urlencode({"labelSelector": selector})
        def confirmed(candidate):
            nonlocal blocker
            if candidate is None:
                return False
            ready, reason = self.revalidate(candidate, job_name, uid, deadline)
            if reason:
                blocker = reason
            nitro_remaining(deadline, NITRO_STARTUP_SECONDS)
            return ready

        try:
            with nitro_deadline(deadline):
                while True:
                    self.active_job(job_name, uid, deadline)
                    snapshot = self.request("GET", self.pods + "?" + query, deadline=deadline)
                    require(not snapshot.get("metadata", {}).get("continue"), "incomplete Nitro EKS pod list")
                    for pod in snapshot.get("items", []):
                        candidate, reason = self.pod_observation(pod, uid)
                        if reason:
                            blocker = reason
                        if confirmed(candidate):
                            return candidate[0]
                    if not polling:
                        version = snapshot.get("metadata", {}).get("resourceVersion")
                        require(isinstance(version, str) and version, "missing Nitro EKS pod resource version")
                        try:
                            candidate = None
                            with closing(self.watch_pods(selector, version, deadline)) as events:
                                for pod in events:
                                    candidate, reason = self.pod_observation(pod, uid)
                                    if reason:
                                        blocker = reason
                                    if candidate:
                                        break
                            if confirmed(candidate):
                                return candidate[0]
                        except NitroWatchForbidden:
                            polling = True
                    time.sleep(nitro_remaining(deadline, backoff))
                    backoff = min(1, backoff * 2)
        except NitroWaitExpired:
            raise ProviderFailure(blocker, "upstream_unavailable") from None
        except (TimeoutError, urllib.error.URLError):
            message = blocker if time.monotonic() >= deadline else "Nitro EKS readiness read failed"
            raise ProviderFailure(message, "upstream_unavailable") from None

    def execute(self, job_name, uid, request, deadline=None):
        if deadline is None:
            deadline = time.monotonic() + NITRO_STARTUP_SECONDS
        pod_name = self.wait_ready(job_name, uid, deadline)
        # Once readiness is established, submit exactly one command with its own budget.
        return nitro_exec(self, pod_name, request)


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
        if request["template"].startswith("image:"):
            mapping = image_mapping(os.environ.get("MAGICMTN_AWS_NITRO_IMAGES", "{}"))
            image = mapping.get(image, image)
        require(isinstance(image, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/_.:@-]{0,511}", image), "invalid Nitro OCI image")
        session = nitro_session(session)
        ttl = request.get("ttl_seconds", 28800)
        require(type(ttl) is int and ttl > 0, "invalid Nitro sandbox TTL")
        return NitroEks().create(image, session, min(ttl, 28800))
    if op == "exec":
        deadline = time.monotonic() + NITRO_STARTUP_SECONDS
        try:
            with nitro_deadline(deadline):
                api = NitroEks(deadline)
                job_name, uid = api.reference(request["sandbox"])
        except NitroWaitExpired:
            raise ProviderFailure(NITRO_NOT_READY, "upstream_unavailable") from None
        except Exception:
            # Botocore may wrap an exception raised by the wall-clock timer.
            if time.monotonic() >= deadline:
                raise ProviderFailure(NITRO_NOT_READY, "upstream_unavailable") from None
            raise
        return api.execute(job_name, uid, request, deadline)
    api = NitroEks()
    job_name, uid = api.reference(request["sandbox"])
    if op == "sandbox_status":
        job = api.request("GET", api.jobs + "/" + job_name, allow_missing=True)
        if job is None:
            return "missing"
        api.owned_job(job, uid)
        if job.get("metadata", {}).get("deletionTimestamp"):
            raise ProviderFailure("Nitro sandbox is terminating", "upstream_unavailable")
        for condition in job.get("status", {}).get("conditions", []):
            if condition.get("status") == "True":
                if condition.get("type") in ("Failed", "Complete"):
                    return "stopped"
                if condition.get("type") in ("FailureTarget", "SuccessCriteriaMet"):
                    raise ProviderFailure("Nitro sandbox is finishing", "upstream_unavailable")
        result = api.request("GET", api.pods + "?" + urlencode({"labelSelector": "job-name=" + job_name}))
        pods = result.get("items")
        require(isinstance(pods, list), "invalid Nitro pod inventory")
        ready = []
        for pod in pods:
            candidate, reason = api.pod_observation(pod, uid)
            if candidate:
                ready.append(candidate)
            if reason:
                raise ProviderFailure(reason, "upstream_unavailable")
        require(len(ready) <= 1, "multiple Nitro sandbox pods are ready")
        if ready:
            return "running"
        raise ProviderFailure(NITRO_NOT_READY, "upstream_unavailable")
    if op == "terminate":
        job = api.request("GET", api.jobs + "/" + job_name, allow_missing=True)
        if job is not None:
            api.owned_job(job, uid)
            api.request("DELETE", api.jobs + "/" + job_name,
                        {"apiVersion": "v1", "kind": "DeleteOptions", "propagationPolicy": "Foreground",
                         "preconditions": {"uid": uid}}, allow_missing=True)
        return None
    raise ProviderFailure("unsupported Nitro operation", "unsupported_operation")
