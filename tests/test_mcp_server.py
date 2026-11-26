import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

import mcp_server


RECORD = {'id':'github:a/b:pr:1', 'visibility':'public', 'title':'Example PR',
          'url':'https://github.com/a/b/pull/1', 'type':'pull_request',
          'updated_at':'2026-01-01', 'body':'example text'}


def test_search_returns_only_bounded_source_evidence():
    record = {**RECORD, 'body':'x' * 700, 'extra':'private metadata'}
    with patch('mcp_server.search', return_value=[record]) as search:
        result = mcp_server.search_context('query')
    search.assert_called_once_with('query')
    assert result == {'items':[{'title':'Example PR', 'url':'https://github.com/a/b/pull/1',
                                'type':'pull_request', 'updated_at':'2026-01-01', 'snippet':'x'*500}]}


def test_invalid_query_and_backend_failure_are_safe():
    with patch('mcp_server.search') as search:
        for query in ('', ' ', 'a', 'x'*101, None, 42):
            assert mcp_server.search_context(query) == {'error':'Search query must contain 2 to 100 characters'}
        search.assert_not_called()
    for error in (OSError('secret'), ValueError('secret'), KeyError('secret')):
        with patch('mcp_server.search', side_effect=error):
            assert mcp_server.search_context('query') == {'error':'Unable to search public sources'}


def test_stdio_protocol_lists_and_calls_search_tool():
    async def run():
        root = Path(__file__).resolve().parent.parent
        # Inject a deterministic public record in the subprocess without connecting to AWS.
        script = "import mcp_server; mcp_server.search=lambda q: [" + repr(RECORD) + "]; mcp_server.server.run(transport='stdio')"
        params = StdioServerParameters(command=sys.executable, args=['-c', script], cwd=root)
        with open(os.devnull, "w") as errors:
            async with stdio_client(params, errlog=errors) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert [tool.name for tool in tools.tools] == ['search_context']
                    response = await session.call_tool('search_context', {'query':'example'})
                    assert not response.is_error
                    result = response.structured_content or json.loads(response.content[0].text)
                    assert result['items'][0]['url'] == RECORD['url']
                    assert result['items'][0]['snippet'] == 'example text'
    asyncio.run(run())


def test_valid_query_boundaries_and_missing_body():
    record = {key:value for key,value in RECORD.items() if key != 'body'}
    with patch('mcp_server.search', return_value=[record]) as search:
        assert mcp_server.search_context('ab')['items'][0]['snippet'] == ''
        assert mcp_server.search_context('x'*100)['items'][0]['snippet'] == ''
        assert [call.args[0] for call in search.call_args_list] == ['ab', 'x'*100]
