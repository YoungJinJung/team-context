"""Local read-only MCP interface for public, source-revalidated records."""
from mcp.server import MCPServer
from storage import search

server = MCPServer('Team Context', instructions='Search public team records and cite the returned source URLs.')


def search_context(query: str) -> dict:
    """Search public PRs and MRs. Results are checked against their original source."""
    if not isinstance(query, str) or not 2 <= len(query.strip()) <= 100:
        return {'error': 'Search query must contain 2 to 100 characters'}
    try:
        records = search(query)
        if not all(isinstance(record, dict) and record.get('visibility') == 'public' for record in records):
            raise ValueError('Non-public record in public search')
        return {'items': [{'title': item['title'], 'url': item['url'],
                           'type': item['type'], 'updated_at': item['updated_at'],
                           'snippet': item.get('body', '')[:500]} for item in records]}
    except Exception:
        return {'error': 'Unable to search public sources'}


server.tool()(search_context)

if __name__ == '__main__':
    server.run(transport='stdio')
