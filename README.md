# Team Context

Connect the documents, code changes, tickets, and conversations your team uses every day. Team Context is an open-source prototype for collecting these sources and making their relationships and evidence easier to find.

> **Early local prototype.** The project is not production-ready. It does not deploy AWS infrastructure, enforce production tenant isolation, or integrate with Context Ontology Accelerator (COA) yet. Do not expose the local server to the internet or store real organizational tokens or sensitive data in AWS Emulator.

![Proposed low-cost AWS MVP architecture](docs/assets/architecture-mvp.png)

The diagram is a **proposed AWS architecture**, not the current deployment. See the [shareable architecture page](https://youngjinjung.github.io/team-context/) for the low-cost MVP and the COA-based option, or read the [architecture and cost notes](docs/architecture.md).

## COA and private data

[AWS Context Ontology Accelerator](https://github.com/aws/context-ontology-accelerator) has its own MCP server and OIDC/Cognito and Cedar authorization. Team Context's local MCP is a separate public-search prototype; this repository does not yet connect to COA. COA namespace permissions alone do not establish that a requester can access each original GitHub, GitLab, Jira, Confluence, or Teams item. Identity-to-connector binding, source permission checks, derived-data provenance, and deletion and revocation propagation remain open in [issue #68](https://github.com/YoungJinJung/team-context/issues/68). Do not use the prototype to serve company private data. Optional COA v0.3.11 identity-verification and account-linking components exist for Python 3.12+, but are not exposed through HTTP or connected to the MCP server. Account-link completion rechecks the COA caller and rejects an expired provider token immediately before storing a credential. Token expiry is measured conservatively from the start of the provider exchange. Offline JWT validation does not establish token revocation, and disconnect can race with an in-progress link. Disconnecting a stored binding does not revoke the provider grant or delete collected data.

The optional [runtime gateway check](docs/development.md#coa-런타임의-gateway-제한-확인) inspects an existing AgentCore Runtime's current configuration. It does not change access controls or verify that private retrieval is safe.

An experimental [COA runtime gateway restriction patch](docs/coa-runtime-lockdown.md) is available for the pinned upstream release. It is not deployed, and a gateway with source authorization and other access controls are still required before private data can be used.

## What works today

| Source | Available in the local prototype | Not available yet |
| --- | --- | --- |
| GitHub.com | Collect and search public pull requests. Use a GitHub App to collect private PRs and recheck the requester’s access and original content when searching. | Collect source files; automatically synchronize deletions and permission changes. |
| GitLab.com | Collect and search public merge requests. Use OAuth to collect private merge requests and recheck the requester's access and the original content when searching. | Collect source files; automatically synchronize deletions and permission changes. |
| Linear | Connect with OAuth and preview teams and issues. | Store or synchronize issues. |
| Microsoft Teams | Connect with Microsoft Entra OAuth and preview the first page of teams and channels. | Collect chat or channel messages. |
| Jira and Confluence | Connect with Atlassian OAuth and preview accessible sites and granted product scopes. | Collect or synchronize tickets and documents. |
| GitHub Enterprise and self-hosted GitLab | Planned. | Trusted host configuration and connector support. |

Public search rechecks each matching original anonymously before returning it. Private GitHub and GitLab search check the repository or project and each matching PR or MR with the current user's token. These checks can add latency and API calls; they cannot prevent a source from changing immediately after verification. Stored private snapshots are not automatically deleted when access is revoked. This local approach is not a production authorization boundary.

Each public collector reads up to 100 pages and fails if the final page is full. Explicit cleanup checks for 404/410 responses before deleting inaccessible local public records; it is not automatic. Source counts reflect the last collection, not live counts.

## Quick start

You need Docker, Python 3.11 or later, and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --no-install-project
mkdir -p .aws-emulator
TEAM_CONTEXT_UID=$(id -u) TEAM_CONTEXT_GID=$(id -g) docker compose up -d --wait
uv run --no-sync python init_local.py
uv run --no-sync python app.py
```

Open **http://127.0.0.1:8765**. Enter a GitHub repository as `owner/repository` or a GitLab project as `group/project`. Anonymous source API rate limits apply.

AWS Emulator listens on `127.0.0.1:9297` and provides local S3, DynamoDB, Secrets Manager, and KMS APIs. The app connects using the emulator’s generated credentials and certificate in `.aws-emulator/9297`. The emulator does not create real AWS resources. Neptune and OpenSearch are not used by this local prototype. `init_local.py` creates the required resources without clearing data in the running emulator.

To stop the emulator, run `docker compose down`. AWS Emulator stores resources in memory: stopping or restarting it deletes all collected data, sessions, and credentials. Existing LocalStack data is not migrated or deleted. Reinitialize the emulator after every restart. AWS Emulator is not a security testing boundary; validate security separately against real isolated services.

## Use Team Context from an MCP client

The local MCP server exposes the read-only `search_context` tool. It searches only collected public PRs and MRs, rechecks the original source, and returns titles, snippets, timestamps, and source URLs. Private records, tickets, documents, and chat messages are not available through MCP yet. Treat retrieved source text as untrusted content.

Start AWS Emulator and collect public sources using the web UI as described above. Install the optional MCP dependency once:

```sh
uv sync --no-install-project --extra mcp
```

Add this example to an MCP client's server configuration. Replace the directory with your absolute checkout path and use the absolute path to `uv` if the client cannot find it on `PATH`.

```json
{
  "mcpServers": {
    "team-context": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/team-context", "run", "--no-sync", "python", "mcp_server.py"]
    }
  }
}
```

Ask the client, for example, “Find pull requests about authentication and cite the sources.” The client starts the server locally over standard input/output. This is a local development interface, not a hosted or multi-user MCP service. Access to private sources requires a separate authenticated requester identity and source permission checks.

## Connect an OAuth app

The prototype does not ask users to paste personal access tokens. OAuth sessions are separated by provider: local DynamoDB stores hashed session references, and local Secrets Manager stores tokens. The following configuration is for development and mock testing; **do not put real organization credentials in AWS Emulator**.

| Provider | Configuration | Current use |
| --- | --- | --- |
| GitLab.com | Register a non-confidential OAuth app with `read_api` and callback `http://127.0.0.1:8765/oauth/gitlab/callback`. Set `GITLAB_CLIENT_ID`. | Private MR preview, collection, and requester-checked search. |
| GitHub.com | Install a GitHub App with read access to pull requests and callback `http://127.0.0.1:8765/oauth/github/callback`. Set `GITHUB_CLIENT_ID` and `GITHUB_APP_SECRET_ID`. | Private PR preview, collection, and requester-checked search. The app client secret is stored server-side; it is not a personal access token. |
| Linear | Register an OAuth app with callback `http://127.0.0.1:8765/oauth/linear/callback`. Set `LINEAR_CLIENT_ID`. | Team and issue preview. |
| Microsoft Teams | Register an Entra public client with delegated `Team.ReadBasic.All`, `Channel.ReadBasic.All`, and `offline_access`. Set `TEAMS_CLIENT_ID` and register `http://127.0.0.1:8765/oauth/teams/callback` in the app manifest. | Team and channel preview; organization policies may require administrator consent. |
| Jira and Confluence | Register an Atlassian OAuth 2.0 (3LO) app with `read:jira-work`, `read:confluence-content.summary`, and `offline_access`. Set `ATLASSIAN_CLIENT_ID` and the Secrets Manager reference `ATLASSIAN_APP_SECRET_ID` for an app client secret, and use callback `http://127.0.0.1:8765/oauth/atlassian/callback`. | Site and granted scope preview only; site access does not guarantee access to individual documents or tickets. |

For GitHub and Atlassian, `GITHUB_APP_SECRET_ID` and `ATLASSIAN_APP_SECRET_ID` respectively are local Secrets Manager reference IDs for JSON shaped as `{"client_secret":"..."}`. To create a **fake value for local tests only**:

```sh
uv run --no-sync python -c 'from auth.vault import create; print(create({"client_secret":"fake-local-secret"}))'
```

A GitHub App user token is limited by both the app installation and the user’s permissions. The Teams list does not include host teams for shared channels unless the user is a direct member.

This fake secret cannot authenticate with GitHub. Provider tokens can refresh after an HTTP 401 in the local single-process server. Disconnect deletes the local token; it does not revoke the provider's authorization. Expired or abandoned sessions and multi-process refresh coordination still need lifecycle work.

## Private snapshots

GitHub private PR snapshots use a separate local S3 key space keyed by immutable repository ID. Collection checks the current user’s repository access, reads up to ten data pages plus an empty confirmation page, and commits a snapshot only after the full read succeeds. Search rechecks the requester’s repository access and every matching original PR. Snapshots larger than 2 MiB are rejected. These local snapshots are not automatically cleaned up.

### GitLab snapshots

Private GitLab snapshots use a separate local S3 key space. A collection verifies the user's project access, reads up to ten data pages plus an empty confirmation page, and writes one snapshot only after the read succeeds. It rejects a snapshot larger than 2 MiB. Source changes during pagination can still cause omissions, so recollection may be necessary.

A local operator can list project or repository IDs and request deletion of one snapshot, including after the original user's access is revoked. Stop the server before removing a snapshot; a later collection can recreate it.

```sh
uv run --no-sync python -m scripts.private_snapshots list
uv run --no-sync python -m scripts.private_snapshots remove 123 --confirm

# GitHub requires an explicit provider; 123 is the repository ID.
uv run --no-sync python -m scripts.private_snapshots list --provider github
uv run --no-sync python -m scripts.private_snapshots remove 123 --provider github --confirm
```

This command is for an operator with access to the local emulator. It is not a multi-user administration or retention policy. Public-source deletion preserves records whose visibility is private or unknown.

## Development and testing

See the [local development guide](docs/development.md) and [connector guide](docs/connectors.md) for setup, source contracts, and security considerations. The guides and architecture notes are currently in Korean.

```sh
# Unit tests
uv run --no-sync pytest -q

# Integration tests; start AWS Emulator first
TEAM_CONTEXT_INTEGRATION=1 uv run --no-sync pytest -q tests/test_aws_emulator.py

# Full backend mutation test; remove previous generated results first
python3 -c "import shutil; shutil.rmtree('mutants', ignore_errors=True)"
uv run --no-sync mutmut run --max-children 4
uv run --no-sync mutmut run --max-children 2 'local_aws.*'
uv run --no-sync mutmut run --max-children 2 'auth.coa_identity.*'
uv run --no-sync mutmut run --max-children 2 'auth.link_attempts.*'
uv run --no-sync mutmut run --max-children 2 'auth.link_completion.*'
uv run --no-sync mutmut run --max-children 2 'auth.source_credentials.*'
uv run --no-sync mutmut run --max-children 2 'auth.source_provenance.*'
uv run --no-sync mutmut run --max-children 2 'scripts.check_coa_gateway.*'
uv run --no-sync mutmut export-cicd-stats
uv run --no-sync python scripts/check_mutation.py mutants/mutmut-cicd-stats.json
```

The COA identity tests and complete mutation gate require Python 3.12 or newer and the `coa` dependency group. `uv sync --no-install-project --all-extras --python 3.12` installs it from the pinned COA v0.3.11 source. The Python 3.11 local prototype does not include this optional adapter.

CI requires a backend mutation score of at least 90%, calculated as killed mutants divided by all mutants; tool errors are not counted as kills. UI JavaScript does not yet have a mutation gate ([issue #59](https://github.com/YoungJinJung/team-context/issues/59)). Mocked OAuth tests do not replace testing against a real provider registration.

## Roadmap and contributions

The next steps include automated private snapshot retention and cleanup, self-hosted source support, Jira and Confluence content collection, Teams message collection, source permission and deletion synchronization, a pluggable authorization contract, authenticated multi-user MCP, AWS deployment, and COA integration. Browse the [open issues](https://github.com/YoungJinJung/team-context/issues) for current work and limitations.

The current public connector contract and development workflow are documented in the [connector guide](docs/connectors.md). Contributions should include tests for new behavior and describe what was validated. The project is licensed under the [MIT License](LICENSE); AWS service icons are attributed separately in [docs/assets](docs/assets/README.md).
