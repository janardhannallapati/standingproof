"""Read-only checks against AWS.

The reader can only call the operations on the read-only list. The AWS SDK is synchronous,
so every call runs in a worker thread. Before any check, the reader confirms that the
credential belongs to the account the check was asked about.
"""

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from standingproof.checks.outcome import CheckOutcome, not_configured, observed, unavailable
from standingproof.checks.redact import keep
from standingproof.checks.registry import (
    EC2_EBS_ENCRYPTION,
    IAM_PASSWORD_POLICY,
    IAM_SUMMARY,
    READ_ONLY_OPERATIONS,
    S3_PUBLIC_ACCESS_BLOCK,
    STS_IDENTITY,
)

# "iam:GetAccountSummary" -> the SDK method to call on that service's client.
_METHODS = {
    STS_IDENTITY: "get_caller_identity",
    IAM_SUMMARY: "get_account_summary",
    IAM_PASSWORD_POLICY: "get_account_password_policy",
    S3_PUBLIC_ACCESS_BLOCK: "get_public_access_block",
    EC2_EBS_ENCRYPTION: "get_ebs_encryption_by_default",
}
_FORBIDDEN = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}
_REJECTED = {
    "InvalidClientTokenId",
    "SignatureDoesNotMatch",
    "ExpiredToken",
    "ExpiredTokenException",
    "AuthFailure",
    "UnrecognizedClientException",
}
_THROTTLED = {
    "Throttling",
    "ThrottlingException",
    "RequestLimitExceeded",
    "TooManyRequestsException",
}


class OperationNotAllowedError(Exception):
    """Code asked for an API operation that is not on the read-only list. A bug."""


class _Refused(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class _Missing(Exception):
    """AWS answered that the thing asked for does not exist. Carries AWS's error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _default_factory(region: str) -> Callable[[str], Any]:
    # Imported here: importing boto3 loads a large service catalogue, and code paths that
    # never check AWS should not pay for it.
    import boto3
    from botocore.config import Config

    session = boto3.session.Session()
    config = Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 2})

    def client(service: str) -> Any:
        return session.client(service, region_name=region, config=config)

    return client


class AwsReader:
    def __init__(self, *, region: str, client_factory: Callable[[str], Any] | None = None) -> None:
        self.region = region
        self._factory = client_factory or _default_factory(region)
        self._clients: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._account: str | None = None

    def _client(self, service: str) -> Any:
        # Using a client from several threads is safe; creating one is not.
        with self._lock:
            if service not in self._clients:
                self._clients[service] = self._factory(service)
            return self._clients[service]

    def _call(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        service = operation.split(":")[0]
        try:
            reply = getattr(self._client(service), _METHODS[operation])(**arguments)
        except NoCredentialsError:
            raise
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", ""))
            # The error message is AWS's text and can name accounts and users: only the
            # code is used.
            if code in _FORBIDDEN:
                raise _Refused("forbidden", "the credential may not read this.") from None
            if code in _REJECTED:
                raise _Refused("credential_rejected", "AWS rejected the credential.") from None
            if code in _THROTTLED:
                raise _Refused("rate_limited", "AWS's rate limit was reached.") from None
            if code.startswith("NoSuch"):
                raise _Missing(code) from None
            raise _Refused("remote_error", f"AWS answered with an error ({code}).") from None
        except BotoCoreError as error:
            raise _Refused(
                "unreachable", f"AWS could not be reached ({type(error).__name__})."
            ) from None
        return dict(reply)

    async def call(self, operation: str, **arguments: Any) -> dict[str, Any]:
        """Make one allow-listed call, in a worker thread."""
        if operation not in READ_ONLY_OPERATIONS or operation not in _METHODS:
            raise OperationNotAllowedError(operation)
        return await asyncio.to_thread(self._call, operation, arguments)

    async def confirm_account(self, expected: str) -> None:
        """Refuse to go on unless the credential belongs to the account being checked."""
        if self._account is None:
            identity = await self.call(STS_IDENTITY)
            self._account = str(identity.get("Account", ""))
        if self._account != expected:
            raise _Refused(
                "wrong_account", f"the credential does not belong to AWS account {expected}."
            )


async def root_mfa(aws: AwsReader, account: str) -> CheckOutcome:
    summary = await aws.call(IAM_SUMMARY)
    raw = keep(summary, ["SummaryMap.AccountMFAEnabled"])
    enabled = raw.get("SummaryMap", {}).get("AccountMFAEnabled") == 1
    has = "has" if enabled else "does not have"
    return observed(
        f"The root user of AWS account {account} {has} multi-factor authentication.",
        {"root_mfa_enabled": enabled},
        raw,
    )


async def password_policy(aws: AwsReader, account: str) -> CheckOutcome:
    try:
        reply = await aws.call(IAM_PASSWORD_POLICY)
    except _Missing:
        # AWS reports an account with no policy as "no such entity". That is a finding.
        return observed(
            f"AWS account {account} has no password policy set for IAM users.",
            {"policy_set": False},
            {},
        )
    raw = keep(
        reply,
        [
            "PasswordPolicy.MinimumPasswordLength",
            "PasswordPolicy.MaxPasswordAge",
            "PasswordPolicy.RequireSymbols",
        ],
    )
    policy = raw.get("PasswordPolicy", {})
    length = policy.get("MinimumPasswordLength")
    max_age = policy.get("MaxPasswordAge")
    expiry = f"passwords expire after {max_age} days" if max_age else "passwords do not expire"
    return observed(
        f"AWS account {account} has a password policy for IAM users: at least {length} "
        f"characters, {expiry}.",
        {
            "policy_set": True,
            "minimum_length": length,
            "max_age_days": max_age,
            "requires_symbols": bool(policy.get("RequireSymbols")),
        },
        raw,
    )


_BLOCK_SETTINGS = {
    "BlockPublicAcls": "block_public_acls",
    "IgnorePublicAcls": "ignore_public_acls",
    "BlockPublicPolicy": "block_public_policy",
    "RestrictPublicBuckets": "restrict_public_buckets",
}


async def s3_public_access_block(aws: AwsReader, account: str) -> CheckOutcome:
    try:
        reply = await aws.call(S3_PUBLIC_ACCESS_BLOCK, AccountId=account)
    except _Missing:
        reply = {}  # no account-wide configuration exists: nothing is blocked
    raw = keep(reply, [f"PublicAccessBlockConfiguration.{name}" for name in _BLOCK_SETTINGS])
    configuration = raw.get("PublicAccessBlockConfiguration", {})
    facts = {fact: configuration.get(name) is True for name, fact in _BLOCK_SETTINGS.items()}
    blocked = all(facts.values())
    verb = "blocks" if blocked else "does not block"
    return observed(
        f"AWS account {account} {verb} all public access to S3 buckets account-wide.",
        {"all_public_access_blocked": blocked, **facts},
        raw,
    )


async def ebs_default_encryption(aws: AwsReader, account: str) -> CheckOutcome:
    reply = await aws.call(EC2_EBS_ENCRYPTION)
    raw = keep(reply, ["EbsEncryptionByDefault"])
    encrypted = raw.get("EbsEncryptionByDefault") is True
    are = "are" if encrypted else "are not"
    return observed(
        f"New EBS disks in AWS account {account} {are} encrypted by default in {aws.region}.",
        {"ebs_encrypted_by_default": encrypted, "region": aws.region},
        raw,
    )


AwsCheck = Callable[[AwsReader, str], Awaitable[CheckOutcome]]


def _safely(check: AwsCheck) -> AwsCheck:
    """Confirm the account first; turn a refusal into a stored outcome, not an error."""

    async def run(aws: AwsReader, account: str) -> CheckOutcome:
        try:
            await aws.confirm_account(account)
            return await check(aws, account)
        except NoCredentialsError:
            return not_configured("no AWS credential is configured.")
        except _Refused as refused:
            return unavailable(refused.reason, refused.detail)
        except _Missing as missing:
            return unavailable("not_found", f"AWS reports it does not exist ({missing.code}).")

    return run


AWS_CHECKS: dict[str, AwsCheck] = {
    "aws.root_mfa": _safely(root_mfa),
    "aws.password_policy": _safely(password_policy),
    "aws.s3_public_access_block": _safely(s3_public_access_block),
    "aws.ebs_default_encryption": _safely(ebs_default_encryption),
}
