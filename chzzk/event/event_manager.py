import asyncio
import logging
from typing import Callable, Coroutine, Any, Optional

from chzzk.event import EventParser
from chzzk.model import ChatCmd

_log = logging.getLogger(__name__)


class EventManager:
    def __init__(self, loop: Optional[asyncio.AbstractEventLoop] = None):
        self.loop = loop or asyncio.get_event_loop()
        self._listeners: dict[str, list[tuple[asyncio.Future, Callable[..., bool]]]] = dict()
        self._extra_event: dict[str, list[Callable[..., Coroutine[Any, Any, Any]]]] = dict()

        self._ready = asyncio.Event()

        handler = {ChatCmd.CONNECTED: self._ready.set}
        self._parser = EventParser(dispatch=self.dispatch, handler=handler)

    async def close(self):
        self._ready.clear()

    async def wait_until_connected(self) -> None:
        """Waits until the client's internal cache is all ready."""
        await self._ready.wait()

    def wait_for(
            self,
            event: str,
            check: Optional[Callable[..., bool]] = None,
            timeout: Optional[float] = None,
    ):
        """Waits for a WebSocket event to be dispatched.

        Parameters
        ----------
        event : str
            The event name.
            For a list of events, read :method:`event`
        check : Optional[Callable[..., bool]],
            A predicate to check what to wait for. The arguments must meet the
            parameters of the event being waited for.
        timeout : Optional[float]
            The number of seconds to wait before timing out and raising
            :exc:`asyncio.TimeoutError`.
        """
        future = self.loop.create_future()

        if check is None:
            def _check(*_):
                return True

            check = _check
        event_name = event.lower()

        if event_name not in self._listeners.keys():
            self._listeners[event_name] = list()
        self._listeners[event_name].append((future, check))
        return asyncio.wait_for(future, timeout=timeout)

    def event(self, coro: Callable[..., Coroutine[Any, Any, Any]]) -> Callable[..., Coroutine[Any, Any, Any]]:
        if not asyncio.iscoroutinefunction(coro):
            raise TypeError("function must be a coroutine.")

        event_name = coro.__name__
        if event_name not in self._listeners.keys():
            self._extra_event[event_name] = list()
        self._extra_event[event_name].append(coro)
        return coro

    def dispatch(self, event: str, *args: Any, **kwargs) -> None:
        method = "on_" + event
        _log.info(f"dispatch:: {method}")
        if method == "on_client_error":
            _log.error(args)

        # wait-for listeners
        if event in self._listeners.keys():
            listeners = self._listeners[event]
            _new_listeners = []

            for index, (future, condition) in enumerate(listeners):
                if future.cancelled():
                    continue

                try:
                    result = condition(*args, **kwargs)
                except Exception as e:
                    future.set_exception(e)
                    continue
                if result:
                    match len(args):
                        case 0:
                            future.set_result(None)
                        case 1:
                            future.set_result(args[0])
                        case _:
                            future.set_result(args)

                _new_listeners.append((future, condition))
            self._listeners[event] = _new_listeners

        # event-listener
        if method not in self._extra_event.keys():
            return

        for coroutine_function in self._extra_event[method]:
            _log.info(f"coroutine_function: {coroutine_function}")
            task = self._schedule_event(coroutine_function, method, *args, **kwargs)
            _log.info(f"task: {task}")
            task.add_done_callback(self._handle_task_result)

    async def _run_event(
            self,
            coro: Callable[..., Coroutine[Any, Any, Any]],
            event_name: str,
            *args: Any,
            **kwargs: Any,
    ) -> None:
        try:
            _log.info(f"run_event: {event_name}")
            await coro(*args, **kwargs)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            try:
                self.dispatch("error", e, *args, **kwargs)
            except asyncio.CancelledError:
                pass

    def _schedule_event(
            self,
            coro: Callable[..., Coroutine[Any, Any, Any]],
            event_name: str,
            *args: Any,
            **kwargs: Any,
    ) -> asyncio.Task:
        wrapped = self._run_event(coro, event_name, *args, **kwargs)
        # Schedules the task
        return self.loop.create_task(wrapped, name=f"chzzk: {event_name}")

    def _handle_task_result(self, task: asyncio.Task) -> None:
        try:
            task.result()  # Task의 결과를 확인
        except Exception as e:
            _log.error(f"Error in task {task.get_name()}: {e}")