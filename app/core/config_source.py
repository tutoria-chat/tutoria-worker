"""
Where configuration comes from, decided once at import (app/core/config.py
calls load_config_source() before Settings is built). Same contract as
FojiApi's ConfigSource and foji-ai-api's app/core/ssm.py:

  AWS_SSM_PREFIX set (e.g. /tutoria/dev/ai-api/)
      every parameter under that path in AWS Systems Manager Parameter Store:
        /tutoria/dev/ai-api/DATABASE_URL  ->  DATABASE_URL
  AZURE_KEYVAULT_URL set (e.g. https://tutoria-dev-ai-xxxx.vault.azure.net/)
      every enabled secret in that Azure Key Vault, read with the managed
      identity (DefaultAzureCredential; AZURE_CLIENT_ID picks the user-assigned
      one). Key Vault names can't hold "_", so "-" stands for it:
        DATABASE-URL  ->  DATABASE_URL
  both set
      refused: startup fails, one source per deployment.
  neither
      the process environment and .env, as in local development.

Each value is put in the environment (over what was there), so
pydantic-settings picks it up. A source that is set but can't be read fails
startup: a task running without its settings would only fail later, worse.
Names and counts are logged, never values.
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# What was loaded, for the startup log line (app.main): logging isn't set up
# yet when this runs at import time.
loaded_from = "local env/.env"

# One call per secret: read a few at a time so a cold start doesn't wait on
# them one after the other.
_KEYVAULT_PARALLEL = 8


class ConfigSourceError(RuntimeError):
    """The configuration source is ambiguous or can't be read: startup must stop."""


def load_config_source() -> None:
    global loaded_from
    ssm_prefix = os.environ.get("AWS_SSM_PREFIX", "")
    vault_url = os.environ.get("AZURE_KEYVAULT_URL", "")
    if ssm_prefix and vault_url:
        raise ConfigSourceError(
            "Both AWS_SSM_PREFIX and AZURE_KEYVAULT_URL are set: configure exactly one "
            "configuration source (AWS SSM on ECS, Azure Key Vault on Container Apps)."
        )
    if ssm_prefix:
        count = load_ssm_params(ssm_prefix)
        loaded_from = f"AWS SSM {ssm_prefix} ({count} parameters)"
    elif vault_url:
        count = load_keyvault_secrets(vault_url)
        loaded_from = f"Azure Key Vault {vault_url} ({count} secrets)"
    else:
        loaded_from = "local env/.env"


def load_ssm_params(prefix: str) -> int:
    import boto3

    region = os.environ.get("AWS_REGION", "us-east-2")
    try:
        client = boto3.client("ssm", region_name=region)
    except Exception as exc:
        raise ConfigSourceError(f"AWS SSM {prefix}: client could not be built ({type(exc).__name__})") from exc

    loaded = 0
    next_token = None
    while True:
        kwargs = {"Path": prefix, "Recursive": True, "WithDecryption": True, "MaxResults": 10}
        if next_token:
            kwargs["NextToken"] = next_token
        try:
            response = client.get_parameters_by_path(**kwargs)
        except Exception as exc:
            raise ConfigSourceError(f"AWS SSM {prefix}: parameters could not be read ({type(exc).__name__})") from exc
        for param in response.get("Parameters", []):
            # /tutoria/dev/ai-api/DATABASE_URL -> DATABASE_URL
            name = param["Name"][len(prefix):].lstrip("/")
            os.environ[name] = param["Value"]
            loaded += 1
        next_token = response.get("NextToken")
        if not next_token:
            break
    logger.info("[Config] Loaded %d parameters from AWS SSM: %s", loaded, prefix)
    return loaded


def env_name_for_secret(secret_name: str) -> str:
    """DATABASE-URL -> DATABASE_URL (Key Vault names allow only letters, digits and "-")."""
    return secret_name.replace("-", "_")


def _keyvault_client(vault_url: str):
    """The vault client and its credential (both closed by the caller)."""
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    # The Azure SDK logs every HTTP call (URL, headers) at INFO. Warnings and
    # errors still show.
    logging.getLogger("azure").setLevel(logging.WARNING)

    credential = DefaultAzureCredential(
        managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID") or None,
        exclude_interactive_browser_credential=True,
    )
    return SecretClient(vault_url=vault_url, credential=credential), credential


def load_keyvault_secrets(vault_url: str) -> int:
    try:
        client, credential = _keyvault_client(vault_url)
    except Exception as exc:
        raise ConfigSourceError(f"Azure Key Vault {vault_url}: client could not be built ({type(exc).__name__})") from exc
    try:
        try:
            names = [p.name for p in client.list_properties_of_secrets() if p.enabled]
        except Exception as exc:
            raise ConfigSourceError(
                f"Azure Key Vault {vault_url}: secrets could not be listed ({type(exc).__name__})"
            ) from exc

        def read(name: str) -> tuple[str, str | None]:
            try:
                return name, client.get_secret(name).value
            except Exception as exc:
                raise ConfigSourceError(
                    f"Azure Key Vault {vault_url}: secret {name} could not be read ({type(exc).__name__})"
                ) from exc

        with ThreadPoolExecutor(max_workers=_KEYVAULT_PARALLEL, thread_name_prefix="keyvault") as pool:
            values = list(pool.map(read, names))
    finally:
        for closeable in (client, credential):
            try:
                closeable.close()
            except Exception:  # noqa: BLE001 — cleanup only
                pass

    loaded = 0
    for name, value in values:
        if value is None:
            continue
        os.environ[env_name_for_secret(name)] = value
        loaded += 1
    logger.info("[Config] Loaded %d secrets from Azure Key Vault: %s", loaded, vault_url)
    return loaded
