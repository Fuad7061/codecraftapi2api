import asyncio
import json
from curl_cffi.requests import AsyncSession

async def main():
    s = AsyncSession(impersonate="chrome120")
    # This won't work because we need a valid session to hit the upstream directly.
    pass
