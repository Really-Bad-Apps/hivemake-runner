import asyncio


async def wait_finished(manager, run_id):
    async with asyncio.timeout(10):
        while run_id in manager.tasks:
            await asyncio.sleep(0.02)
