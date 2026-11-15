import asyncio
import time
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError, NoCredentialsError
from botocore.stub import Stubber

from standingproof.checks.aws import AWS_CHECKS, AwsReader, OperationNotAllowedError

ACCOUNT = "123456789012"
SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"  # noqa: S105 - AWS's documented example
IDENTITY = {
    "Account": ACCOUNT,
    "Arn": f"arn:aws:iam::{ACCOUNT}:user/reader",
    "UserId": "AIDAEXAMPLE",
}


class Stubbed:
    """boto3 clients that answer from a script and make no network call."""

    def __init__(self) -> None:
        self.clients: dict[str, Any] = {}
        self.stubs: dict[str, Stubber] = {}
        for service in ("sts", "iam", "s3control", "ec2"):
            client = boto3.client(
                service,
                region_name="eu-west-1",
                aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
                aws_secret_access_key=SECRET,
            )
            self.clients[service] = client
            self.stubs[service] = Stubber(client)
            self.stubs[service].activate()

    def reader(self) -> AwsReader:
        return AwsReader(region="eu-west-1", client_factory=self.clients.__getitem__)

    def identity(self, account: str = ACCOUNT) -> None:
        self.stubs["sts"].add_response("get_caller_identity", {**IDENTITY, "Account": account})


@pytest.fixture
def aws() -> Iterator[Stubbed]:
    stubbed = Stubbed()
    yield stubbed
    for stub in stubbed.stubs.values():
        stub.assert_no_pending_responses()


async def test_root_mfa_on_and_off(aws: Stubbed) -> None:
    aws.identity()
    aws.stubs["iam"].add_response(
        "get_account_summary", {"SummaryMap": {"AccountMFAEnabled": 1, "Users": 3}}
    )
    aws.stubs["iam"].add_response("get_account_summary", {"SummaryMap": {"AccountMFAEnabled": 0}})
    reader = aws.reader()

    on = await AWS_CHECKS["aws.root_mfa"](reader, ACCOUNT)
    off = await AWS_CHECKS["aws.root_mfa"](reader, ACCOUNT)

    assert on.status == "observed"
    assert on.facts == {"root_mfa_enabled": True}
    assert (
        on.statement == f"The root user of AWS account {ACCOUNT} has multi-factor authentication."
    )
    assert on.raw == {"SummaryMap": {"AccountMFAEnabled": 1}}
    assert off.facts == {"root_mfa_enabled": False}
    assert "does not have" in off.statement


async def test_password_policy(aws: Stubbed) -> None:
    aws.identity()
    aws.stubs["iam"].add_response(
        "get_account_password_policy",
        {
            "PasswordPolicy": {
                "MinimumPasswordLength": 14,
                "RequireSymbols": True,
                "MaxPasswordAge": 90,
            }
        },
    )

    outcome = await AWS_CHECKS["aws.password_policy"](aws.reader(), ACCOUNT)

    assert outcome.facts == {
        "policy_set": True,
        "minimum_length": 14,
        "max_age_days": 90,
        "requires_symbols": True,
    }
    assert outcome.statement == (
        f"AWS account {ACCOUNT} has a password policy for IAM users: at least 14 characters, "
        "passwords expire after 90 days."
    )


async def test_no_password_policy_is_an_observation(aws: Stubbed) -> None:
    aws.identity()
    aws.stubs["iam"].add_client_error("get_account_password_policy", "NoSuchEntity")

    outcome = await AWS_CHECKS["aws.password_policy"](aws.reader(), ACCOUNT)

    assert outcome.status == "observed"
    assert outcome.facts == {"policy_set": False}
    assert outcome.statement == f"AWS account {ACCOUNT} has no password policy set for IAM users."


async def test_s3_public_access_block(aws: Stubbed) -> None:
    aws.identity()
    block = {
        "BlockPublicAcls": True,
        "IgnorePublicAcls": True,
        "BlockPublicPolicy": True,
        "RestrictPublicBuckets": True,
    }
    aws.stubs["s3control"].add_response(
        "get_public_access_block",
        {"PublicAccessBlockConfiguration": block},
        {"AccountId": ACCOUNT},
    )
    aws.stubs["s3control"].add_response(
        "get_public_access_block",
        {"PublicAccessBlockConfiguration": {**block, "BlockPublicPolicy": False}},
        {"AccountId": ACCOUNT},
    )
    aws.stubs["s3control"].add_client_error(
        "get_public_access_block", "NoSuchPublicAccessBlockConfiguration"
    )
    reader = aws.reader()
    check = AWS_CHECKS["aws.s3_public_access_block"]

    full, partial, none = [await check(reader, ACCOUNT) for _ in range(3)]

    assert full.facts["all_public_access_blocked"] is True
    assert "blocks all public access" in full.statement
    assert partial.facts["all_public_access_blocked"] is False
    assert partial.facts["block_public_policy"] is False
    assert "does not block all public access" in partial.statement
    assert none.status == "observed"
    assert none.facts["all_public_access_blocked"] is False


async def test_ebs_default_encryption_names_the_region(aws: Stubbed) -> None:
    aws.identity()
    aws.stubs["ec2"].add_response("get_ebs_encryption_by_default", {"EbsEncryptionByDefault": True})

    outcome = await AWS_CHECKS["aws.ebs_default_encryption"](aws.reader(), ACCOUNT)

    assert outcome.facts == {"ebs_encrypted_by_default": True, "region": "eu-west-1"}
    assert outcome.statement == (
        f"New EBS disks in AWS account {ACCOUNT} are encrypted by default in eu-west-1."
    )


async def test_a_credential_for_another_account_observes_nothing(aws: Stubbed) -> None:
    aws.identity(account="999999999999")

    outcome = await AWS_CHECKS["aws.root_mfa"](aws.reader(), ACCOUNT)

    assert outcome.status == "unavailable"
    assert outcome.reason == "wrong_account"
    # Which account the credential does belong to is not ours to record.
    assert "999999999999" not in str(outcome)


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("AccessDenied", "forbidden"),
        ("UnauthorizedOperation", "forbidden"),
        ("InvalidClientTokenId", "credential_rejected"),
        ("ExpiredToken", "credential_rejected"),
        ("Throttling", "rate_limited"),
        ("ServiceFailure", "remote_error"),
    ],
)
async def test_each_kind_of_refusal_is_unavailable_with_its_reason(
    aws: Stubbed, code: str, reason: str
) -> None:
    aws.identity()
    aws.stubs["iam"].add_client_error("get_account_summary", code, "a message naming " + ACCOUNT)

    outcome = await AWS_CHECKS["aws.root_mfa"](aws.reader(), ACCOUNT)

    assert outcome.status == "unavailable"
    assert outcome.reason == reason
    assert outcome.facts == {}


async def test_no_credentials_is_not_configured_and_unreachable_is_unavailable() -> None:
    def no_credentials(service: str) -> Any:
        raise NoCredentialsError()

    def unreachable(service: str) -> Any:
        raise EndpointConnectionError(endpoint_url="https://sts.eu-west-1.amazonaws.com")

    missing = await AWS_CHECKS["aws.root_mfa"](
        AwsReader(region="eu-west-1", client_factory=no_credentials), ACCOUNT
    )
    down = await AWS_CHECKS["aws.root_mfa"](
        AwsReader(region="eu-west-1", client_factory=unreachable), ACCOUNT
    )

    assert missing.status == "not_configured"
    assert down.reason == "unreachable"


async def test_the_reader_refuses_an_operation_that_is_not_on_the_read_only_list(
    aws: Stubbed,
) -> None:
    reader = aws.reader()

    for operation in ["iam:DeleteUser", "iam:ListUsers", "s3:GetObject", "GET /orgs/{org}"]:
        with pytest.raises(OperationNotAllowedError):
            await reader.call(operation)


async def test_the_account_is_confirmed_once_per_reader_not_once_per_check(aws: Stubbed) -> None:
    aws.identity()  # one reply only: a second identity call would fail the stub
    aws.stubs["iam"].add_response("get_account_summary", {"SummaryMap": {"AccountMFAEnabled": 1}})
    aws.stubs["ec2"].add_response(
        "get_ebs_encryption_by_default", {"EbsEncryptionByDefault": False}
    )
    reader = aws.reader()

    first = await AWS_CHECKS["aws.root_mfa"](reader, ACCOUNT)
    second = await AWS_CHECKS["aws.ebs_default_encryption"](reader, ACCOUNT)

    assert (first.status, second.status) == ("observed", "observed")


async def test_aws_calls_run_off_the_event_loop() -> None:
    class Slow:
        def get_caller_identity(self) -> dict[str, str]:
            time.sleep(0.3)  # the SDK is synchronous: it blocks whichever thread runs it
            return IDENTITY

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        await AwsReader(region="eu-west-1", client_factory=lambda _: Slow()).call(
            "sts:GetCallerIdentity"
        )
    finally:
        task.cancel()

    assert ticks >= 10
