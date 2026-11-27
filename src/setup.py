"""Read-only account selection, prepared image validation, and Bedrock bindings."""
from contextlib import contextmanager

SETUP_OPTIONS = {
    "region": "MAGICMTN_AWS_REGION",
    "bedrock_role_arn": "MAGICMTN_AWS_BEDROCK_ROLE_ARN",
    "sandbox_backend": "MAGICMTN_AWS_SANDBOX_BACKEND",
    "sandbox_platform": "MAGICMTN_AWS_SANDBOX_PLATFORM",
    "microvm_images": "MAGICMTN_AWS_MICROVM_IMAGES",
    "nitro_images": "MAGICMTN_AWS_NITRO_IMAGES",
    "nitro_eks_cluster": "MAGICMTN_AWS_NITRO_EKS_CLUSTER",
    "nitro_eks_namespace": "MAGICMTN_AWS_NITRO_EKS_NAMESPACE",
}


@contextmanager
def setup_environment(values):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def aws_region():
    region = (os.environ.get("MAGICMTN_AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
              or os.environ.get("AWS_REGION") or "us-east-2")
    require(isinstance(region, str) and re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", region), "invalid AWS region")
    return region


def bedrock_region():
    region = (os.environ.get("MAGICMTN_AWS_BEDROCK_REGION") or os.environ.get("MAGICMTN_AWS_REGION")
              or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-2")
    require(region == "us-east-2", "Bedrock inference must enter through us-east-2")
    return region


def caller_account():
    identity = client("sts", connect_timeout=3, read_timeout=5).get_caller_identity()
    account = identity.get("Account", "")
    require(isinstance(account, str) and re.fullmatch(r"[0-9]{12}", account), "invalid AWS account identity")
    expected = os.environ.get("MAGICMTN_AWS_ACCOUNT_ID")
    require(not expected or expected == account, "AWS account identity changed")
    return account


def verify_connection():
    require(CREDENTIAL is not None, "AWS credentials are required")
    account = caller_account()
    if os.environ.get("MAGICMTN_AWS_NITRO_EKS_CLUSTER_ARN"):
        validate_nitro_connection()
    if os.environ.get("MAGICMTN_AWS_BEDROCK_ROLE_ARN"):
        bedrock_client()
    return account


def validate_nitro_connection():
    api = NitroEks()
    namespace = api.request("GET", "/api/v1/namespaces/" + api.namespace)
    require(namespace.get("metadata", {}).get("name") == api.namespace, "unexpected Nitro namespace identity")
    runtime = api.request("GET", "/apis/node.k8s.io/v1/runtimeclasses/isolated-sandbox")
    require(runtime.get("metadata", {}).get("name") == "isolated-sandbox", "Nitro runtime class is missing")
    api.namespace_uid = nitro_session(namespace.get("metadata", {}).get("uid"))
    api.runtime_uid = nitro_session(runtime.get("metadata", {}).get("uid"))
    for key, identity in (("MAGICMTN_AWS_NITRO_NAMESPACE_UID", api.namespace_uid),
                          ("MAGICMTN_AWS_NITRO_RUNTIME_UID", api.runtime_uid)):
        require(not os.environ.get(key) or os.environ[key] == identity, "Nitro configured resource identity changed")
    return api


def image_mapping(value):
    try:
        mapping = json.loads(value)
    except (ValueError, TypeError):
        raise ProviderFailure("invalid AWS image mapping JSON") from None
    require(isinstance(value, str) and len(value.encode()) <= 60 * 1024
            and isinstance(mapping, dict) and len(mapping) <= 128
            and all(isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/_.:@-]{0,511}", key)
                    and isinstance(target, str) and target and len(target) <= 512
                    and not any(c.isspace() or ord(c) < 32 for c in target)
                    for key, target in mapping.items()), "invalid AWS prepared image mapping")
    return mapping


def configure_connection(request):
    require(CREDENTIAL is not None, "AWS setup requires explicit credentials")
    options = request.get("options", {})
    allowed = {"region", "bedrock_role_arn"} if request.get("bedrock_only") else set(SETUP_OPTIONS)
    require(isinstance(options, dict) and set(options) <= allowed
            and all(isinstance(value, str) and len(value) <= 1024 and "\0" not in value
                    for value in options.values()), "invalid AWS setup options")
    configuration = {key: os.environ[key] for key in SETUP_OPTIONS.values() if key in os.environ}
    for key in ("MAGICMTN_AWS_ACCOUNT_ID", "MAGICMTN_AWS_BEDROCK_REGION", "MAGICMTN_AWS_NITRO_EKS_CLUSTER_ARN",
                "MAGICMTN_AWS_NITRO_CA_SHA256", "MAGICMTN_AWS_NITRO_NAMESPACE_UID", "MAGICMTN_AWS_NITRO_RUNTIME_UID",
                "MAGICMTN_AWS_MICROVM_EGRESS_CONNECTORS", "MAGICMTN_AWS_MICROVM_EXECUTION_ROLE_ARN"):
        if key in os.environ:
            configuration[key] = os.environ[key]
    for option, value in options.items():
        key = SETUP_OPTIONS[option]
        if option in ("region", "nitro_eks_cluster", "nitro_eks_namespace"):
            require(key not in configuration or configuration[key] == value,
                    "configured AWS resource identity cannot be replaced")
        if option in ("microvm_images", "nitro_images"):
            previous = image_mapping(configuration.get(key, "{}"))
            supplied = image_mapping(value)
            require(all(name not in previous or previous[name] == target for name, target in supplied.items()),
                    "configured AWS image mapping cannot be replaced")
            previous.update(supplied)
            value = json.dumps(previous)
        configuration[key] = value
    configuration.setdefault("MAGICMTN_AWS_REGION", aws_region())
    configuration.setdefault("MAGICMTN_AWS_BEDROCK_REGION", "us-east-2")
    if not request.get("bedrock_only"):
        configuration.setdefault("MAGICMTN_AWS_SANDBOX_BACKEND", "microvm")
    with setup_environment(configuration):
        aws_region()
        bedrock_region()
        account = caller_account()
        configuration["MAGICMTN_AWS_ACCOUNT_ID"] = account
        role = configuration.get("MAGICMTN_AWS_BEDROCK_ROLE_ARN")
        require(not role or re.fullmatch(r"arn:aws:iam::[0-9]{12}:role/[\w+=,.@/-]+", role), "invalid Bedrock role ARN")
        require(not request.get("bedrock_only") or role, "Bedrock-only setup requires a data-plane role")
        if role:
            bedrock_client()
        backend = configuration.get("MAGICMTN_AWS_SANDBOX_BACKEND", "microvm")
        require(backend in ("microvm", "nitro-eks"), "invalid AWS sandbox backend")
        platform = configuration.get("MAGICMTN_AWS_SANDBOX_PLATFORM", "linux/arm64" if backend == "microvm" else "linux/amd64")
        require(platform in ("linux/arm64", "linux/amd64")
                and (backend != "microvm" or platform == "linux/arm64"), "unsupported AWS sandbox platform")
        if not request.get("bedrock_only"):
            configuration["MAGICMTN_AWS_SANDBOX_PLATFORM"] = platform
        for key in ("MAGICMTN_AWS_MICROVM_IMAGES", "MAGICMTN_AWS_NITRO_IMAGES"):
            if key in configuration:
                image_mapping(configuration[key])
        if backend == "nitro-eks" and not request.get("bedrock_only"):
            api = validate_nitro_connection()
            require(api.scope.split("/")[2] == account, "Nitro cluster belongs to another AWS account")
            configuration["MAGICMTN_AWS_NITRO_EKS_CLUSTER_ARN"] = api.cluster_arn
            configuration["MAGICMTN_AWS_NITRO_CA_SHA256"] = api.ca_sha256
            configuration["MAGICMTN_AWS_NITRO_NAMESPACE_UID"] = api.namespace_uid
            configuration["MAGICMTN_AWS_NITRO_RUNTIME_UID"] = api.runtime_uid
            configuration.setdefault("MAGICMTN_AWS_NITRO_EKS_NAMESPACE", api.namespace)
    return {"account": account, "configuration": configuration}


def immutable_image(images, platform):
    require(isinstance(images, dict) and isinstance(images.get(platform), dict), "image has no manifest for " + platform)
    image = images[platform]
    require(set(image) == {"repository", "digest"} and isinstance(image["repository"], str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/_.:-]{0,511}", image["repository"])
            and isinstance(image["digest"], str) and re.fullmatch(r"sha256:[0-9a-f]{64}", image["digest"]),
            "an immutable OCI image reference is required")
    return image, image["repository"] + "@" + image["digest"]


def scoped_image_arn(arn, api, account):
    require(isinstance(arn, str) and re.fullmatch(
        rf"arn:{re.escape(api.meta.partition)}:lambda:{re.escape(api.meta.region_name)}:{account}:microvm-image:[A-Za-z0-9-]+", arn),
        "unexpected MicroVM image account or region")


def prepare_aws_image(images):
    backend = os.environ.get("MAGICMTN_AWS_SANDBOX_BACKEND", "microvm")
    require(backend in ("microvm", "nitro-eks"), "invalid AWS sandbox backend")
    platform = os.environ.get("MAGICMTN_AWS_SANDBOX_PLATFORM", "linux/arm64" if backend == "microvm" else "linux/amd64")
    require(platform in ("linux/arm64", "linux/amd64") and (backend != "microvm" or platform == "linux/arm64"), "unsupported AWS sandbox platform")
    image, reference = immutable_image(images, platform)
    key = "MAGICMTN_AWS_MICROVM_IMAGES" if backend == "microvm" else "MAGICMTN_AWS_NITRO_IMAGES"
    mapping = image_mapping(os.environ.get(key, "{}"))
    require(reference in mapping, "configure a prepared AWS image mapping for the requested OCI digest")
    selected = mapping[reference]
    account = caller_account()
    if backend == "microvm":
        identifier, sep, version = selected.partition("@")
        require(identifier and len(identifier) <= 256 and (not sep or bool(version)), "invalid prepared MicroVM image")
        api = client("lambda-microvms")
        if not sep:
            current = api.get_microvm_image(imageIdentifier=identifier)
            scoped_image_arn(current.get("imageArn"), api, account)
            require(current.get("state") in ("CREATED", "UPDATED"), "MicroVM image is unavailable")
            version = current.get("latestActiveImageVersion")
            require(isinstance(version, str) and version, "MicroVM image has no active version")
        result = api.get_microvm_image_version(imageIdentifier=identifier, imageVersion=version)
        arn = result.get("imageArn")
        scoped_image_arn(arn, api, account)
        require((identifier.startswith("arn:") and identifier == arn) or arn.endswith(":" + identifier), "MicroVM image identity changed")
        require(result.get("imageVersion") == version and result.get("state") == "SUCCESSFUL"
                and result.get("status") == "ACTIVE", "MicroVM image version is unavailable")
        require(any(cpu.get("architecture") == "ARM_64" for cpu in result.get("cpuConfigurations", [])), "MicroVM image architecture mismatch")
        selected = arn + "@" + version
    else:
        match = re.fullmatch(r"([0-9]{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com/([a-z0-9._/-]+)@(sha256:[0-9a-f]{64})", selected)
        require(match is not None and match[1] == account and match[2] == aws_region(), "prepared Nitro image must use an account-scoped ECR digest")
        api = client("ecr")
        require(api.meta.region_name == match[2], "prepared Nitro image belongs to another region")
        result = api.describe_images(registryId=account, repositoryName=match[3], imageIds=[{"imageDigest": match[4]}])
        details = result.get("imageDetails", [])
        require(len(details) == 1 and details[0].get("imageDigest") == match[4]
                and details[0].get("registryId") == account and details[0].get("repositoryName") == match[3], "prepared Nitro image identity changed")
    mapping[reference] = selected
    return {"image": image, "platform": platform, "artifact": selected,
            "configuration": {key: json.dumps(mapping), "MAGICMTN_AWS_SANDBOX_BACKEND": backend,
                              "MAGICMTN_AWS_SANDBOX_PLATFORM": platform}}


def bedrock_selection(request):
    operation = request["operation"]
    require(operation in ("deployment_create", "deployment_start", "deployment_status", "deployment_stop", "deployment_delete"),
            "invalid Bedrock selection operation")
    role = os.environ.get("MAGICMTN_AWS_BEDROCK_ROLE_ARN")
    require(not role or re.fullmatch(r"arn:aws:iam::[0-9]{12}:role/[\w+=,.@/-]+", role), "invalid Bedrock role ARN")
    parts = None
    if operation == "deployment_create":
        spec = request["spec"]
        profile = spec.get("model")
        require(spec.get("configuration") in ({}, None), "Bedrock selection requires no deployment configuration")
    else:
        reference = request.get("resource", {}).get("provider_id", "")
        parts = reference.split("/") if isinstance(reference, str) else []
        require(len(parts) == 5 and parts[0] == "bedrock" and parts[2:4] == ["us-east-2", "profiles"]
                and re.fullmatch(r"[0-9]{12}", parts[1]), "invalid Bedrock selection reference")
        profile = parts[4]
    require(isinstance(profile, str) and re.fullmatch(r"us\.openai\.[A-Za-z0-9.:-]+", profile), "invalid US Bedrock OpenAI profile")
    bedrock_region()
    source_account = caller_account()
    # The profile belongs to the invocation role's account, which may differ from the caller.
    account = role.split(":")[4] if role else source_account
    require(parts is None or parts[1] == account, "Bedrock selection belongs to another account or region")
    if operation == "deployment_delete":
        return None  # Release the host's selection; the upstream profile is shared.
    if operation == "deployment_stop":
        raise ProviderFailure("Bedrock profiles use serverless invocation", "unsupported_operation")
    result = bedrock_client("bedrock").get_inference_profile(inferenceProfileIdentifier=profile)
    require(result.get("inferenceProfileId") == profile and result.get("status") == "ACTIVE", "Bedrock inference profile is unavailable")
    expected = f"arn:aws:bedrock:us-east-2:{account}:inference-profile/{profile}"
    require(result.get("inferenceProfileArn") == expected, "Bedrock inference profile identity changed")
    return {"resource": {"provider_id": f"bedrock/{account}/us-east-2/profiles/{profile}"},
            "model": profile, "inference_model": profile, "state": "ready", "ready_replicas": None}
