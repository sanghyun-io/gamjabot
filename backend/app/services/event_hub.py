"""In-process wakeups; SQLite remains the durable replay authority."""

import asyncio
from collections import defaultdict


class EventHub:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.subscribers = defaultdict(set)

    def subscribe(self, room_id: int) -> asyncio.Queue:
        queue = asyncio.Queue(maxsize=1)
        self.subscribers[room_id].add(queue)
        return queue

    def unsubscribe(self, room_id: int, queue: asyncio.Queue) -> None:
        self.subscribers[room_id].discard(queue)
        if not self.subscribers[room_id]:
            del self.subscribers[room_id]

    def publish(self, room_id: int) -> None:
        def wake():
            for queue in tuple(self.subscribers.get(room_id, ())):
                if queue.empty():
                    queue.put_nowait(None)
        self.loop.call_soon_threadsafe(wake)
