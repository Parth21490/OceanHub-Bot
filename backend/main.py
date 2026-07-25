"""
OceanHub Phase 0 — Dry Run Entrypoint
"""
import os
import sys
import asyncio

# Global dry run mode safety flag
DRY_RUN_MODE = True

if __name__ == "__main__":
    print("==============================================")
    print("  OceanHub Phase 0 — DRY RUN MODE ACTIVE")
    print("  Calculations & logic run, but API orders blocked.")
    print("==============================================")
    
    # Run the main server loop
    from server import main as server_main
    asyncio.run(server_main())
