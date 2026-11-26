# COA Context Manager gateway restriction

The optional patch in `patches/coa-v0.3.11-gateway-only.patch` changes the AWS Context Ontology Accelerator (COA) v0.3.11 Context Manager runtime to accept JWT invocations only through one configured AgentCore Gateway. The patch fails CDK synthesis if the gateway ARN is absent, malformed, or in a different account or Region. It adds the runtime's `AllowedWorkloadConfiguration.HostingEnvironments` property; it does not create the gateway or its authorization policy.

**Do not deploy these patches to an existing COA installation as-is.** The unmodified COA web application, data layer, and MCP server invoke the Context Manager directly. The separate MCP patch below routes the MCP execution tools through a Gateway, but the web and data-layer paths are not yet routed. Restricting the runtime before routing these paths through a gateway will interrupt them. A gateway reachable by users must enforce source authorization before forwarding requests; a gateway alone does not prevent a caller from reaching the Context Manager through it. The patches also do not restrict other COA APIs, database access, S3, older runtime endpoints, or a separate runtime. Private data must remain disabled until those paths and complete source provenance are enforced and tested. See [issue #73](https://github.com/YoungJinJung/team-context/issues/73).

To review and test the patch in a disposable COA checkout:

```sh
git clone https://github.com/aws/context-ontology-accelerator.git coa-review
git -C coa-review checkout 5dd33d7eb21d9af3de8fe4f03558eca992a4104a
git -C coa-review apply --check /absolute/path/to/team-context/patches/coa-v0.3.11-gateway-only.patch
git -C coa-review apply /absolute/path/to/team-context/patches/coa-v0.3.11-gateway-only.patch
git -C coa-review apply --check /absolute/path/to/team-context/patches/coa-v0.3.11-mcp-gateway.patch
git -C coa-review apply /absolute/path/to/team-context/patches/coa-v0.3.11-mcp-gateway.patch
cd coa-review
pnpm install --filter 'coa-infra...' --frozen-lockfile --ignore-scripts
pnpm --filter @coa/shared build
pnpm --filter coa-infra exec jest test/services/serve-stack.test.ts --runInBand --coverage=false --testNamePattern='ServeStack - gateway-only runtime'
pnpm --filter coa-infra exec jest test/services/mcp-stack.test.ts --runInBand --coverage=false
```

When these patches are used in a purpose-built deployment, CDK must receive `team_context_gateway_arn` and `team_context_gateway_target` context values for an HTTP runtime target on a gateway in the runtime's account and Region. The MCP client constructs the gateway URL from those trusted settings, forwards the caller's JWT and session header, and does not fall back to direct runtime access. The gateway must be configured to forward the original user's JWT and to enforce source authorization before invoking the Context Manager. Route every legitimate Context Manager caller through an authenticated gateway with verified authorization before deploying the restriction. The [runtime gateway check](development.md#coa-런타임의-gateway-제한-확인) can inspect current endpoint versions afterwards, but cannot replace live direct-invocation tests or verify the other access paths.

The COA integration CI applies both patches to the pinned upstream commit, checks the synthesized runtimes and MCP transport, and runs focused security mutations. It does not emulate AgentCore or deploy AWS resources. AWS documents the workload restriction and gateway target limitations in its [runtime authorization guide](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-oauth.html) and [runtime target guide](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-http-runtime.html).
