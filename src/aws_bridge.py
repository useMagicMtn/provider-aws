"""Embedded Boto3 transport. No SDK retries and no upstream error-body disclosure."""
import json
import os
import re
import sys
import time
import urllib.error
import uuid
import urllib.request
from urllib.parse import urlsplit

LIMIT = 768 * 1024
CREDENTIAL = None


class ProviderFailure(Exception):
    def __init__(self, message, code="invalid_request"):
        self.message, self.code = message, code


def require(condition, message):
    if not condition:
        raise ProviderFailure(message)


def name(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", value), "invalid SageMaker resource name")
    return value


def target(value, managed=False):
    parts = value.split("/", 3) if isinstance(value, str) else []
    require(len(parts) == 4 and parts[0] in (("sagemaker",) if managed else ("sagemaker", "endpoints")) and parts[2] == "models", "invalid SageMaker reference")
    name(parts[1])
    require(bool(parts[3]) and len(parts[3]) <= 512 and not any(c.isspace() or ord(c) < 32 for c in parts[3]), "invalid served model")
    return parts[1], parts[3]


def microvm_id(request):
    value = request["sandbox"]["provider_id"]
    require(isinstance(value, str) and re.fullmatch(r"(?:microvm|mvm)-[A-Za-z0-9-]{1,128}", value), "invalid MicroVM ID")
    return value


def credential_kwargs():
    if CREDENTIAL is None:
        return {}
    return {"aws_access_key_id": CREDENTIAL["AWS_ACCESS_KEY_ID"],
            "aws_secret_access_key": CREDENTIAL["AWS_SECRET_ACCESS_KEY"]}


def client(service, connect_timeout=5, read_timeout=70):
    import boto3
    from botocore.config import Config
    # Explicit broker credentials take precedence over the legacy host credential chain.
    # Disable retry even on mutations and model invocations.
    region = os.environ.get("MAGICMTN_AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if CREDENTIAL is not None:
        region = region or os.environ.get("AWS_REGION")
    config = Config(connect_timeout=connect_timeout, read_timeout=read_timeout,
                    retries={"total_max_attempts": 1}, ignore_configured_endpoint_urls=True)
    return boto3.client(service, region_name=region, config=config, **credential_kwargs())


def bedrock_client(service="bedrock-runtime"):
    import boto3
    from botocore.config import Config
    region = bedrock_region()
    config = Config(signature_version="v4", connect_timeout=5, read_timeout=70,
                    retries={"total_max_attempts": 1}, ignore_configured_endpoint_urls=True)
    role = os.environ.get("MAGICMTN_AWS_BEDROCK_ROLE_ARN")
    session = boto3.Session(region_name=region, **credential_kwargs())
    if role:
        require(re.fullmatch(r"arn:aws:iam::[0-9]{12}:role/[\w+=,.@/-]+", role), "invalid Bedrock role ARN")
        credentials = session.client("sts", config=config).assume_role(
            RoleArn=role, RoleSessionName="magicmtn-bedrock", DurationSeconds=900)["Credentials"]
        session = boto3.Session(region_name=region, aws_access_key_id=credentials["AccessKeyId"],
                                aws_secret_access_key=credentials["SecretAccessKey"],
                                aws_session_token=credentials["SessionToken"])
    # Never use a configured endpoint or a bearer API key in place of the role.
    require(service in ("bedrock-runtime", "bedrock"), "invalid Bedrock service")
    return session.client(service, config=config,
                          endpoint_url=f"https://{service}.us-east-2.amazonaws.com")


def bedrock_infer(request):
    profile = request["target"]
    require(isinstance(profile, str) and re.fullmatch(r"us\.openai\.[A-Za-z0-9.:-]+", profile), "invalid US Bedrock OpenAI profile")
    require(request["body"].get("model") == profile and request["body"].get("stream") is False,
            "Bedrock model or stream mismatch")
    body = json.dumps(request["body"]).encode()
    require(len(body) <= 4 * 1024 * 1024, "inference payload exceeds 4 MiB")
    require(not request.get("require_bedrock_role") or bool(os.environ.get("MAGICMTN_AWS_BEDROCK_ROLE_ARN")),
            "managed Bedrock inference requires a data-plane role")
    response = bedrock_client().invoke_model(modelId=profile, contentType="application/json",
                                              accept="application/json", body=body)
    stream = response["body"]
    try:
        raw = stream.read(LIMIT + 1)
    finally:
        stream.close()
    require(len(raw) <= LIMIT, "inference response too large")
    return json.loads(raw)


def missing(error):
    # SageMaker uses ValidationException for missing resources as well as bad input.
    code = error.response.get("Error", {}).get("Code", "")
    message = error.response.get("Error", {}).get("Message", "").lower()
    return code == "ResourceNotFound" or (code == "ValidationException" and
           ("could not find" in message or "does not exist" in message or "not found" in message))


def describe(api, operation, **kwargs):
    from botocore.exceptions import ClientError
    try:
        return getattr(api, operation)(**kwargs)
    except ClientError as error:
        if missing(error):
            return None
        raise


def deployment_result(ref, state, replicas=None):
    _, model = target(ref, managed=True)
    return {"resource": {"provider_id": ref}, "model": model,
            "inference_model": ref, "state": state, "ready_replicas": replicas}


def owned(api, arn):
    tags = api.list_tags(ResourceArn=arn).get("Tags", [])
    require({"Key": "magicmtn-provider", "Value": "aws-v1"} in tags,
            "resource is not managed by this provider")


def config_for(api, resource):
    cfg = describe(api, "describe_endpoint_config", EndpointConfigName=resource)
    if cfg:
        owned(api, cfg["EndpointConfigArn"])
        variants = cfg["ProductionVariants"]
        require(len(variants) == 1 and variants[0]["ModelName"] == resource,
                "unexpected endpoint configuration")
    return cfg


def endpoint_for(api, resource):
    ep = describe(api, "describe_endpoint", EndpointName=resource)
    if ep:
        owned(api, ep["EndpointArn"])
        require(ep["EndpointConfigName"] == resource, "endpoint configuration changed outside this provider")
    return ep


def endpoint_state(ep):
    return {"Creating": "starting", "Updating": "starting", "SystemUpdating": "starting",
            "InService": "ready", "Deleting": "stopping", "Failed": "failed",
            "OutOfService": "failed", "UpdateRollbackFailed": "failed"}.get(ep["EndpointStatus"], "unknown")


def create_deployment(spec, api):
    resource = name(spec["name"])
    model = spec["model"]
    ref = f"sagemaker/{resource}/models/{model}"
    target(ref, managed=True)
    cfg = spec["configuration"]
    allowed = {"mode", "image", "execution_role_arn", "model_data_url", "environment",
               "instance_type", "initial_instance_count", "memory_size_mb", "max_concurrency", "provisioned_concurrency"}
    require(isinstance(cfg, dict) and not cfg.keys() - allowed, "unsupported deployment configuration")
    require(cfg.get("mode") in ("realtime", "serverless"), "mode must be realtime or serverless")
    require(isinstance(cfg.get("image"), str) and cfg["image"], "SageMaker-compatible ECR image required")
    require(isinstance(cfg.get("execution_role_arn"), str) and re.fullmatch(r"arn:aws[a-z-]*:iam::[0-9]{12}:role/[\w+=,.@/-]+", cfg["execution_role_arn"]), "execution role ARN required")
    container = {"Image": cfg["image"]}
    if "model_data_url" in cfg:
        require(isinstance(cfg["model_data_url"], str) and cfg["model_data_url"].startswith("s3://"), "model_data_url must be an S3 URI")
        container["ModelDataUrl"] = cfg["model_data_url"]
    if "environment" in cfg:
        require(isinstance(cfg["environment"], dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in cfg["environment"].items()), "environment must contain strings")
        container["Environment"] = cfg["environment"]
    variant = {"VariantName": "AllTraffic", "ModelName": resource}
    if cfg["mode"] == "serverless":
        require(not {"instance_type", "initial_instance_count"} & cfg.keys(), "serverless does not accept instances or GPUs")
        memory, concurrency = cfg.get("memory_size_mb", 6144), cfg.get("max_concurrency", 10)
        require(type(memory) is int and memory in range(1024, 6145, 1024), "invalid serverless memory size")
        require(type(concurrency) is int and 1 <= concurrency <= 200, "invalid serverless concurrency")
        serverless = {"MemorySizeInMB": memory, "MaxConcurrency": concurrency}
        if "provisioned_concurrency" in cfg:
            pc = cfg["provisioned_concurrency"]
            require(type(pc) is int and 1 <= pc <= concurrency, "invalid provisioned concurrency")
            serverless["ProvisionedConcurrency"] = pc
        variant["ServerlessConfig"] = serverless
    else:
        require(not {"memory_size_mb", "max_concurrency", "provisioned_concurrency"} & cfg.keys(), "realtime does not accept serverless settings")
        require(isinstance(cfg.get("instance_type"), str) and re.fullmatch(r"ml\.[a-z0-9]+\.[a-z0-9]+", cfg["instance_type"]), "instance_type required")
        count = cfg.get("initial_instance_count", 1)
        require(type(count) is int and 1 <= count <= 100, "invalid instance count")
        variant.update(InstanceType=cfg["instance_type"], InitialInstanceCount=count, InitialVariantWeight=1.0)
    tags = [{"Key": "magicmtn-provider", "Value": "aws-v1"}, {"Key": "magicmtn-model", "Value": model}]
    require(len(model) <= 256, "deployment model name exceeds AWS tag limit")
    # No rollback on an ambiguous failure; names allow the caller to reconcile.
    api.create_model(ModelName=resource, ExecutionRoleArn=cfg["execution_role_arn"], PrimaryContainer=container, Tags=tags)
    api.create_endpoint_config(EndpointConfigName=resource, ProductionVariants=[variant], Tags=tags)
    return deployment_result(ref, "stopped", 0)


def manage(request):
    api = client("sagemaker")
    op = request["operation"]
    if op == "deployment_create":
        return create_deployment(request["spec"], api)
    ref = request["resource"]["provider_id"]
    resource, model = target(ref, managed=True)
    cfg = config_for(api, resource)
    ep = endpoint_for(api, resource)
    if op == "deployment_status":
        if ep:
            replicas = None
            if all("CurrentInstanceCount" in v for v in ep.get("ProductionVariants", [])) and ep.get("ProductionVariants"):
                replicas = sum(v["CurrentInstanceCount"] for v in ep["ProductionVariants"])
            return deployment_result(ref, endpoint_state(ep), replicas)
        remaining_model = describe(api, "describe_model", ModelName=resource)
        if remaining_model:
            owned(api, remaining_model["ModelArn"])
        return deployment_result(ref, "stopped" if cfg else ("failed" if remaining_model else "deleted"), 0)
    if op == "deployment_start":
        require(cfg is not None, "endpoint configuration missing; reconcile incomplete creation")
        if ep:
            return deployment_result(ref, endpoint_state(ep))
        tags = [{"Key": "magicmtn-provider", "Value": "aws-v1"}, {"Key": "magicmtn-model", "Value": model}]
        api.create_endpoint(EndpointName=resource, EndpointConfigName=resource, Tags=tags)
        return deployment_result(ref, "starting")
    if op == "deployment_stop":
        if ep and ep["EndpointStatus"] != "Deleting":
            api.delete_endpoint(EndpointName=resource)
        return deployment_result(ref, "stopping" if ep else "stopped", None if ep else 0)
    if op == "deployment_delete":
        # Explicit two-phase teardown avoids deleting a configuration still in use.
        require(ep is None, "stop and poll until stopped before deleting deployment artifacts")
        if cfg:
            api.delete_endpoint_config(EndpointConfigName=resource)
        mdl = describe(api, "describe_model", ModelName=resource)
        if mdl:
            owned(api, mdl["ModelArn"])
            api.delete_model(ModelName=resource)
        return None
    raise ProviderFailure("unsupported deployment operation", "unsupported_operation")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def endpoint_url(endpoint, identifier, region):
    require(isinstance(endpoint, str), "MicroVM endpoint missing")
    value = endpoint if endpoint.startswith("https://") else "https://" + endpoint
    parsed = urlsplit(value)
    # Trust endpoint identity fetched from AWS, never persisted caller metadata.
    expected = f"{identifier}.lambda-microvm.{region}.on.aws"
    # Live endpoints use an independent UUID; documented examples use the ID.
    host = parsed.hostname or ""
    independent = re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.lambda-microvm\." + re.escape(region) + r"\.on\.aws", host)
    require(parsed.scheme == "https" and (host == expected or independent) and parsed.port is None
            and not parsed.username and not parsed.password and parsed.path in ("", "/")
            and not parsed.query and not parsed.fragment, "unexpected MicroVM endpoint")
    return value.rstrip("/")


def dispatch(request):
    op = request["operation"]
    if op == "verify_account":
        return verify_connection()
    if op == "configure":
        return configure_connection(request)
    if op == "prepare_image":
        return prepare_aws_image(request.get("images"))
    if op == "sandbox_create":
        template = request.get("template", "")
        backend = os.environ.get("MAGICMTN_AWS_SANDBOX_BACKEND", "microvm")
        require(backend in ("microvm", "nitro-eks"), "invalid AWS sandbox backend")
        if isinstance(template, str) and (template.startswith("nitro-eks:") or
                (template.startswith("image:") and backend == "nitro-eks")):
            return nitro_dispatch(request)
    elif op in ("terminate", "exec", "suspend", "resume", "sandbox_status"):
        identifier = request.get("sandbox", {}).get("provider_id", "")
        if isinstance(identifier, str) and identifier.startswith("nitro-eks/"):
            return nitro_dispatch(request)
    if op.startswith("deployment_"):
        selected = request.get("spec", {}).get("model", "") if op == "deployment_create" else request.get("resource", {}).get("provider_id", "")
        prefix = "us.openai." if op == "deployment_create" else "bedrock/"
        if isinstance(selected, str) and selected.startswith(prefix):
            return bedrock_selection(request)
        return manage(request)
    if op == "infer":
        if request["target"].startswith("us.openai."):
            return bedrock_infer(request)
        require(not os.environ.get("MAGICMTN_AWS_BEDROCK_ROLE_ARN"), "Bedrock credentials cannot invoke SageMaker")
        endpoint, _ = target(request["target"])
        body = json.dumps(request["body"]).encode()
        require(len(body) <= 4 * 1024 * 1024, "inference payload exceeds 4 MiB")
        response = client("sagemaker-runtime").invoke_endpoint(EndpointName=endpoint, ContentType="application/json", Accept="application/json", Body=body)
        stream = response["Body"]
        try:
            raw = stream.read(LIMIT + 1)
        finally:
            stream.close()
        require(len(raw) <= LIMIT, "inference response too large")
        return json.loads(raw)
    if op == "sandbox_create":
        ttl_seconds = request.get("ttl_seconds", 28800)
        require(type(ttl_seconds) is int and ttl_seconds > 0, "invalid sandbox TTL")
        template = request["template"]
        require(isinstance(template, str) and template.startswith(("microvm:", "image:")), "use image:<mapped OCI reference>|<session UUID> or microvm:<image-id-or-arn>[@version]|<session UUID>")
        prefix, contents = template.split(":", 1)
        parts = contents.rsplit("|", 1)
        require(len(parts) == 2, "sandbox template requires a session UUID")
        image, session = parts
        source_image = image
        if prefix == "image":
            require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/_.:@-]{0,511}", image), "invalid OCI reference")
            mapping = json.loads(os.environ.get("MAGICMTN_AWS_MICROVM_IMAGES", "{}"))
            require(isinstance(mapping, dict) and isinstance(mapping.get(image), str), "OCI image is not mapped in MAGICMTN_AWS_MICROVM_IMAGES; build the agent MicroVM image first")
            image = mapping[image]
        try:
            session = uuid.UUID(session)
        except (ValueError, AttributeError):
            raise ProviderFailure("invalid session UUID") from None
        require(session.int != 0 and image and len(image) <= 256 and not any(c.isspace() for c in image), "invalid image or session")
        image, sep, version = image.partition("@")
        api = client("lambda-microvms")
        options = {"imageIdentifier": image, "clientToken": str(session),
                   "ingressNetworkConnectors": [f"arn:{api.meta.partition}:lambda:{api.meta.region_name}:aws:network-connector:aws-network-connector:ALL_INGRESS"],
                   "idlePolicy": {"autoResumeEnabled": True, "maxIdleDurationSeconds": 900, "suspendedDurationSeconds": 28800},
                   "maximumDurationInSeconds": min(ttl_seconds, 28800)}
        if sep:
            require(bool(version), "image version required after @")
            options["imageVersion"] = version
        # Outbound network and runtime IAM role are host policy, not model input.
        if os.environ.get("MAGICMTN_AWS_MICROVM_EGRESS_CONNECTORS"):
            options["egressNetworkConnectors"] = json.loads(os.environ["MAGICMTN_AWS_MICROVM_EGRESS_CONNECTORS"])
        if os.environ.get("MAGICMTN_AWS_MICROVM_EXECUTION_ROLE_ARN"):
            options["executionRoleArn"] = os.environ["MAGICMTN_AWS_MICROVM_EXECUTION_ROLE_ARN"]
        r = api.run_microvm(**options)
        return {"provider_id": r["microvmId"], "status": r["state"], "create_time_ms": None,
                "metadata": {"region": api.meta.region_name, "image": image, "source_image": source_image, "session": str(session)}}
    identifier = microvm_id(request)
    api = client("lambda-microvms")
    region = (request["sandbox"].get("metadata") or {}).get("region")
    require(region is None or region == api.meta.region_name, "sandbox belongs to a different AWS region")
    if op == "sandbox_status":
        from botocore.exceptions import ClientError
        try:
            current = api.get_microvm(microvmIdentifier=identifier)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                return "missing"
            raise
        require(current.get("microvmId") == identifier, "MicroVM identity changed")
        state = current.get("state")
        if state == "TERMINATED":
            return "missing"
        if state == "SUSPENDED":
            return "stopped"
        if state == "RUNNING":
            return "running"
        raise ProviderFailure("MicroVM is not ready", "upstream_unavailable")
    if op in ("terminate", "suspend", "resume"):
        getattr(api, op + "_microvm")(microvmIdentifier=identifier)
        return None
    if op == "exec":
        r = api.get_microvm(microvmIdentifier=identifier)
        require(r["state"] not in ("TERMINATING", "TERMINATED"), "MicroVM has terminated")
        root = endpoint_url(r["endpoint"], identifier, api.meta.region_name)
        token = api.create_microvm_auth_token(microvmIdentifier=identifier, expirationInMinutes=1, allowedPorts=[{"port": 8080}])["authToken"]["X-aws-proxy-auth"]
        body = {k: request[k] for k in ("command", "args", "timeout_ms", "max_output_bytes")}
        req = urllib.request.Request(root + "/exec", data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "X-aws-proxy-auth": token, "X-aws-proxy-port": "8080"})
        opener = urllib.request.build_opener(NoRedirect())
        # Readiness is eventually consistent; retry only the read-only health probe.
        deadline = time.monotonic() + 15
        while True:
            health = urllib.request.Request(root + "/health", headers={"X-aws-proxy-auth": token, "X-aws-proxy-port": "8080"})
            try:
                with opener.open(health, timeout=3) as response:
                    health_body = response.read(4097)
                    require(len(health_body) <= 4096 and json.loads(health_body).get("agent_version") == 1, "image does not contain the MagicMtn agent")
                break
            except urllib.error.HTTPError as error:
                error.close()
                if error.code not in (502, 503, 504) or time.monotonic() >= deadline:
                    raise
            except urllib.error.URLError:
                if time.monotonic() >= deadline:
                    raise
            time.sleep(0.2)
        # Never replay command execution.
        with opener.open(req, timeout=40) as response:
            raw = response.read(128 * 1024 + 1)
            require(len(raw) <= 128 * 1024, "sandbox response too large")
            return json.loads(raw)
    raise ProviderFailure("unsupported operation", "unsupported_operation")


def main():
    global CREDENTIAL
    try:
        payload = json.loads(sys.stdin.buffer.read(1024 * 1024 + 1))
        request, CREDENTIAL = payload
        require(CREDENTIAL is None or (isinstance(CREDENTIAL, dict)
                and set(CREDENTIAL) == {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}
                and all(isinstance(value, str) and value for value in CREDENTIAL.values())),
                "invalid AWS credential bundle")
        result = dispatch(request)
    except ProviderFailure as error:
        result = {"error": {"code": error.code, "message": error.message}}
    except ImportError:
        result = {"error": {"code": "invalid_request", "message": "install provider-aws/requirements.txt in MAGICMTN_AWS_PYTHON"}}
    except Exception as error:
        # Report safe codes only; AWS and HTTP messages can contain credentials/input.
        code = getattr(error, "response", {}).get("Error", {}).get("Code", "")
        safe = code if re.fullmatch(r"[A-Za-z0-9]{1,64}", code) else type(error).__name__
        result = {"error": {"code": "upstream_unavailable", "message": f"AWS operation failed ({safe}); check credentials, region, service availability and resource state; reconcile before retrying"}}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
