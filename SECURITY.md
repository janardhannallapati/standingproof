# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through the "Report a vulnerability" button on the
Security tab of this repository. Do not open a public issue.

You will get an acknowledgement within 5 working days. Please include steps to reproduce and the
version or commit affected.

## Supported versions

The project is pre-1.0. Only the latest commit on `main` is supported.

## Credentials the product uses

Standingproof never needs write access to your systems. Give it credentials that cannot write.

| Credential | Used for | Permissions it needs |
|---|---|---|
| `ANTHROPIC_API_KEY` | Drafting answers | An API key |
| `SP_GITHUB_TOKEN` | Live checks of GitHub settings | A fine-grained token: repository **Administration: read** and **Metadata: read**; organisation **Administration: read** |
| AWS credentials (the SDK's standard variables, or a role) | Live checks of AWS settings | `iam:GetAccountSummary`, `iam:GetAccountPasswordPolicy`, `s3:GetAccountPublicAccessBlock`, `ec2:GetEbsEncryptionByDefault` |
| `VOYAGE_API_KEY` (optional) | A hosted embedding model, if you choose one | An API key |

AWS policy for the checks:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "iam:GetAccountSummary",
        "iam:GetAccountPasswordPolicy",
        "s3:GetAccountPublicAccessBlock",
        "ec2:GetEbsEncryptionByDefault"
      ],
      "Resource": "*"
    }
  ]
}
```

In this version credentials are read from the environment, one set for the whole deployment.
Because they are shared, which organisations and accounts they may be used on is set by the
operator (`SP_GITHUB_ORGS`, `SP_AWS_ACCOUNTS`), not by a caller, and each such scope can be
connected by one tenant only. Do not run several tenants that should not see each other's
settings against one set of credentials; per-tenant credentials are planned.
They are not stored in the database, and they are not written to logs, error messages or
stored evidence.

## What leaves your machine

| Destination | What is sent | When |
|---|---|---|
| Anthropic | The question and the sources the agent retrieved | When the agent drafts an answer |
| GitHub, AWS | Read requests for the settings listed above | When a check runs |
| Voyage AI | Library text and search queries | Only if you select a Voyage embedding model |

Nothing else. The local embedding model's runtime ships with usage telemetry enabled by its
vendor; Standingproof disables it, and a test fails if the runtime records anything.
