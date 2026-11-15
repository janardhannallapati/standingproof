"""The checks that exist. Nothing outside this registry can be run.

A check reads one setting from one system. Each declares the API operations it uses, and
every one of those must be on READ_ONLY_OPERATIONS; the clients refuse anything that is not.
"""

from dataclasses import dataclass
from typing import Literal

System = Literal["github", "aws"]
ScopeKind = Literal["org", "repo", "account"]

GET_ORG = "GET /orgs/{org}"
GET_REPO = "GET /repos/{owner}/{repo}"
GET_BRANCH_PROTECTION = "GET /repos/{owner}/{repo}/branches/{branch}/protection"
STS_IDENTITY = "sts:GetCallerIdentity"
IAM_SUMMARY = "iam:GetAccountSummary"
IAM_PASSWORD_POLICY = "iam:GetAccountPasswordPolicy"  # noqa: S105 - an API name, not a password
S3_PUBLIC_ACCESS_BLOCK = "s3control:GetPublicAccessBlock"
EC2_EBS_ENCRYPTION = "ec2:GetEbsEncryptionByDefault"

# Every call any check may make. All of them only read.
READ_ONLY_OPERATIONS: frozenset[str] = frozenset(
    {
        GET_ORG,
        GET_REPO,
        GET_BRANCH_PROTECTION,
        STS_IDENTITY,
        IAM_SUMMARY,
        IAM_PASSWORD_POLICY,
        S3_PUBLIC_ACCESS_BLOCK,
        EC2_EBS_ENCRYPTION,
    }
)


@dataclass(frozen=True)
class CheckDef:
    name: str
    system: System
    # What the scope must be: an organisation, "owner/repository", or a 12-digit account id.
    scope_kind: ScopeKind
    description: str
    operations: tuple[str, ...]


def _registry(*checks: CheckDef) -> dict[str, CheckDef]:
    return {check.name: check for check in checks}


CHECKS: dict[str, CheckDef] = _registry(
    CheckDef(
        "github.org_two_factor",
        "github",
        "org",
        "Whether the GitHub organisation requires two-factor sign-in for its members.",
        (GET_ORG,),
    ),
    CheckDef(
        "github.branch_protection",
        "github",
        "repo",
        "Whether the repository's default branch is protected and how many approving reviews "
        "a change needs before it can be merged.",
        (GET_REPO, GET_BRANCH_PROTECTION),
    ),
    CheckDef(
        "github.secret_scanning",
        "github",
        "repo",
        "Whether GitHub secret scanning is switched on for the repository.",
        (GET_REPO,),
    ),
    CheckDef(
        "aws.root_mfa",
        "aws",
        "account",
        "Whether the AWS account's root user has multi-factor authentication.",
        (STS_IDENTITY, IAM_SUMMARY),
    ),
    CheckDef(
        "aws.password_policy",
        "aws",
        "account",
        "The AWS account's password policy for IAM users: minimum length and rotation.",
        (STS_IDENTITY, IAM_PASSWORD_POLICY),
    ),
    CheckDef(
        "aws.s3_public_access_block",
        "aws",
        "account",
        "Whether the AWS account blocks public access to S3 buckets account-wide.",
        (STS_IDENTITY, S3_PUBLIC_ACCESS_BLOCK),
    ),
    CheckDef(
        "aws.ebs_default_encryption",
        "aws",
        "account",
        "Whether new EBS disks are encrypted by default in the configured AWS region.",
        (STS_IDENTITY, EC2_EBS_ENCRYPTION),
    ),
)
