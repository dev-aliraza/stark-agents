#!/usr/bin/env python3
"""A minimal stdio MCP server, so the MCP example needs no external services.

Stark starts this automatically because `inventory-agent/AGENT.md` lists it under `mcp:`
with `enable: true`. It is launched with the agent's own directory as the working
directory, which is why `args: ["server.py"]` resolves.

Note `purge_warehouse` below: it is exposed by the server but filtered out by
`exclude:` in the frontmatter, so the model never sees it.
"""

from mcp.server.mcpserver import MCPServer

# log_level keeps the server's own INFO chatter off the shared stderr.
mcp = MCPServer("warehouse", log_level="WARNING")

STOCK = {
    "ATL-PRO-001": {"quantity": 1_420, "warehouse": "Rotterdam", "reorder_at": 500},
    "ATL-LITE-002": {"quantity": 180, "warehouse": "Singapore", "reorder_at": 400},
    "ATL-MINI-003": {"quantity": 0, "warehouse": "Newark", "reorder_at": 250},
}


@mcp.tool()
def list_skus() -> dict:
    """List every SKU in the warehouse catalogue."""
    return {"skus": sorted(STOCK)}


@mcp.tool()
def check_stock(sku: str) -> dict:
    """Return the stock level for one SKU.

    Args:
        sku: The product SKU, for example ATL-PRO-001.
    """
    record = STOCK.get(sku.strip().upper())
    if record is None:
        return {"error": f"unknown sku '{sku}'", "available": sorted(STOCK)}
    return {
        "sku": sku.strip().upper(),
        **record,
        "needs_reorder": record["quantity"] <= record["reorder_at"],
    }


@mcp.tool()
def purge_warehouse(warehouse: str) -> dict:
    """Delete all stock records for a warehouse. Excluded in AGENT.md."""
    return {"purged": warehouse}


if __name__ == "__main__":
    mcp.run()
