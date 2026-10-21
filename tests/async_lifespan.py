"""Keep task-bound ASGI lifespan context managers in one owning asyncio task."""
import asyncio

async def start_lifespan(test, context):
    ready = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()

    async def own_lifespan():
        try:
            async with context:
                ready.set_result(None)
                await stop.wait()
        except BaseException as error:
            if not ready.done():
                ready.set_exception(error)
            raise

    task = asyncio.create_task(own_lifespan())

    async def close():
        stop.set()
        await asyncio.wait_for(task, 30)

    test.addAsyncCleanup(close)
    await ready
