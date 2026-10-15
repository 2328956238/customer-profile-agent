"""Single-process bounded admission and reclaimable customer locks."""
import asyncio
from contextlib import asynccontextmanager


class Overloaded(RuntimeError):
    pass


async def run_blocking(callback, *args, **kwargs):
    """Do not release caller-owned capacity while cancelled work still runs.

    Python cannot interrupt a working thread. Each callback must bound its own
    work/I/O; cancellation is propagated only after that thread has completed.
    """
    task = asyncio.create_task(asyncio.to_thread(callback, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()  # Retrieve any failure without replacing cancellation.
        raise


class Admission:
    def __init__(self, global_limit=8, tenant_limit=4):
        if any(type(n) is not int or n <= 0 for n in (global_limit, tenant_limit)):
            raise ValueError('concurrency limits must be positive integers')
        self.global_limit, self.tenant_limit = global_limit, tenant_limit
        self.active = 0
        self.tenants = {}

    @asynccontextmanager
    async def enter(self, tenant):
        # No await between admission check and reservation: atomic in one event loop.
        if self.active >= self.global_limit or self.tenants.get(tenant, 0) >= self.tenant_limit:
            raise Overloaded('request capacity reached; retry later')
        self.active += 1
        self.tenants[tenant] = self.tenants.get(tenant, 0) + 1
        try:
            yield
        finally:
            self.active -= 1
            self.tenants[tenant] -= 1
            if not self.tenants[tenant]:
                del self.tenants[tenant]


class CustomerLocks:
    def __init__(self):
        self.entries = {}

    @asynccontextmanager
    async def hold(self, customer, timeout):
        entry = self.entries.setdefault(customer, [asyncio.Lock(), 0])
        entry[1] += 1
        acquired = False
        try:
            await asyncio.wait_for(entry[0].acquire(), timeout=timeout)
            acquired = True
            yield
        finally:
            if acquired:
                entry[0].release()
            entry[1] -= 1
            if not entry[1]:
                self.entries.pop(customer, None)
