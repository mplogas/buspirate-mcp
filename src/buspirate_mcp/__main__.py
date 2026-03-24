"""Allow running as: python -m buspirate_mcp"""

from buspirate_mcp.server import main

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
