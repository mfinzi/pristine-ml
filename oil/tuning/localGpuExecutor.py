import os
#import torch.multiprocessing as multiprocessing
from concurrent import futures
import torch
torch.multiprocessing.set_start_method("spawn",force="True")#.get_context('spawn')
multiprocessing = torch.multiprocessing

#multiprocessing.set_start_method('spawn')
#import multiprocessing
import traceback

import torch

"""Implements ProcessPoolExecutor.
The follow diagram and text describe the data-flow through the system:
|======================= In-process =====================|== Out-of-process ==|
+----------+     +----------+       +--------+     +-----------+    +---------+
|          |  => | Work Ids |    => |        |  => | Call Q    | => |         |
|          |     +----------+       |        |     +-----------+    |         |
|          |     | ...      |       |        |     | ...       |    |         |
|          |     | 6        |       |        |     | 5, call() |    |         |
|          |     | 7        |       |        |     | ...       |    |         |
| Process  |     | ...      |       | Local  |     +-----------+    | Process |
|  Pool    |     +----------+       | Worker |                      |  #1..n  |
| Executor |                        | Thread |                      |         |
|          |     +----------- +     |        |     +-----------+    |         |
|          | <=> | Work Items | <=> |        | <=  | Result Q  | <= |         |
|          |     +------------+     |        |     +-----------+    |         |
|          |     | 6: call()  |     |        |     | ...       |    |         |
|          |     |    future  |     |        |     | 4, result |    |         |
|          |     | ...        |     |        |     | 3, except |    |         |
+----------+     +------------+     +--------+     +-----------+    +---------+
Executor.submit() called:
- creates a uniquely numbered _WorkItem and adds it to the "Work Items" dict
- adds the id of the _WorkItem to the "Work Ids" queue
Local worker thread:
- reads work ids from the "Work Ids" queue and looks up the corresponding
  WorkItem from the "Work Items" dict: if the work item has been cancelled then
  it is simply removed from the dict, otherwise it is repackaged as a
  _CallItem and put in the "Call Q". New _CallItems are put in the "Call Q"
  until "Call Q" is full. NOTE: the size of the "Call Q" is kept small because
  calls placed in the "Call Q" can no longer be cancelled with Future.cancel().
- reads _ResultItems from "Result Q", updates the future stored in the
  "Work Items" dict and deletes the dict entry
Process #1..n:
- reads _CallItems from "Call Q", executes the calls, and puts the resulting
  _ResultItems in "Result Q"
"""

__author__ = 'Brian Quinlan (brian@sweetapp.com)'

import atexit
import os
from concurrent.futures import _base
import queue
from queue import Full
#import multiprocessing
from multiprocessing import SimpleQueue
from multiprocessing.connection import wait
import threading
import weakref
from functools import partial
import itertools
import traceback

# Workers are created as daemon threads and processes. This is done to allow the
# interpreter to exit when there are still idle processes in a
# ProcessPoolExecutor's process pool (i.e. shutdown() was not called). However,
# allowing workers to die with the interpreter has two undesirable properties:
#   - The workers would still be running during interpreter shutdown,
#     meaning that they would fail in unpredictable ways.
#   - The workers could be killed while evaluating a work item, which could
#     be bad if the callable being evaluated has external side-effects e.g.
#     writing to a file.
#
# To work around this problem, an exit handler is installed which tells the
# workers to exit when their work queues are empty and then waits until the
# threads/processes finish.

_threads_queues = weakref.WeakKeyDictionary()
_shutdown = False

def _python_exit():
    global _shutdown
    _shutdown = True
    items = list(_threads_queues.items())
    for t, q in items:
        q.put(None)
    for t, q in items:
        t.join()

# Controls how many more calls than processes will be queued in the call queue.
# A smaller number will mean that processes spend more time idle waiting for
# work while a larger number will make Future.cancel() succeed less frequently
# (Futures in the call queue cannot be cancelled).
EXTRA_QUEUED_CALLS = 1

# Hack to embed stringification of remote traceback in local traceback

class _RemoteTraceback(Exception):
    def __init__(self, tb):
        self.tb = tb
    def __str__(self):
        return self.tb

class _ExceptionWithTraceback:
    def __init__(self, exc, tb):
        tb = traceback.format_exception(type(exc), exc, tb)
        tb = ''.join(tb)
        self.exc = exc
        self.tb = '\n"""\n%s"""' % tb
    def __reduce__(self):
        return _rebuild_exc, (self.exc, self.tb)

def _rebuild_exc(exc, tb):
    exc.__cause__ = _RemoteTraceback(tb)
    return exc

class _WorkItem(object):
    def __init__(self, future, fn, args, kwargs):
        self.future = future
        self.fn = fn
        self.args = args
        self.kwargs = kwargs

class _ResultItem(object):
    def __init__(self, work_id, exception=None, result=None):
        self.work_id = work_id
        self.exception = exception
        self.result = result

class _CallItem(object):
    def __init__(self, work_id, fn, args, kwargs):
        self.work_id = work_id
        self.fn = fn
        self.args = args
        self.kwargs = kwargs

def _get_chunks(*iterables, chunksize):
    """ Iterates over zip()ed iterables in chunks. """
    it = zip(*iterables)
    while True:
        chunk = tuple(itertools.islice(it, chunksize))
        if not chunk:
            return
        yield chunk

def _process_chunk(fn, chunk):
    """ Processes a chunk of an iterable passed to map.
    Runs the function passed to map() on a chunk of the
    iterable passed to map.
    This function is run in a separate process.
    """
    return [fn(*args) for args in chunk]

def _process_worker(call_queue, result_queue,gpu_set,gpu_pool):
    """Evaluates calls from call_queue and places the results in result_queue.
    This worker is run in a separate process.
    Args:
        call_queue: A multiprocessing.Queue of _CallItems that will be read and
            evaluated by the worker.
        result_queue: A multiprocessing.Queue of _ResultItems that will written
            to by the worker.
        shutdown: A multiprocessing.Event that will be set as a signal to the
            worker that it should exit when call_queue is empty.
    """
    while True:
        call_item = call_queue.get(block=True)
        if call_item is None:
            # Wake up queue management thread
            result_queue.put(os.getpid())
            return
        try:
            #print(id(call_queue))
            while True:
                gpu_id = gpu_pool.get(block=True)
                if gpu_id in gpu_set:
                    break
            os.environ["CUDA_DEVICE_ORDER"]="PCI_BUS_ID"
            os.environ["CUDA_VISIBLE_DEVICES"]=gpu_id#str(gpu_id)
            #print(gpu_id)
            r = call_item.fn(*call_item.args, **call_item.kwargs)
        except BaseException as e:
            exc = _ExceptionWithTraceback(e, e.__traceback__)
            result_queue.put(_ResultItem(call_item.work_id, exception=exc))
        else:
            result_queue.put(_ResultItem(call_item.work_id,
                                         result=r))
        # if gpu_id in gpu_set: # Only return the gpu to the pool if allowed
        gpu_pool.put(gpu_id)
        # else:
        #     print(f"gpu {gpu_id} was not returned to the pool")

def _add_call_item_to_queue(pending_work_items,
                            work_ids,
                            call_queue):
    """Fills call_queue with _WorkItems from pending_work_items.
    This function never blocks.
    Args:
        pending_work_items: A dict mapping work ids to _WorkItems e.g.
            {5: <_WorkItem...>, 6: <_WorkItem...>, ...}
        work_ids: A queue.Queue of work ids e.g. Queue([5, 6, ...]). Work ids
            are consumed and the corresponding _WorkItems from
            pending_work_items are transformed into _CallItems and put in
            call_queue.
        call_queue: A multiprocessing.Queue that will be filled with _CallItems
            derived from _WorkItems.
    """
    while True:
        if call_queue.full():
            return
        try:
            work_id = work_ids.get(block=False)
        except queue.Empty:
            return
        else:
            work_item = pending_work_items[work_id]

            if work_item.future.set_running_or_notify_cancel():
                call_queue.put(_CallItem(work_id,
                                         work_item.fn,
                                         work_item.args,
                                         work_item.kwargs),
                               block=True)
            else:
                del pending_work_items[work_id]
                continue

def _queue_management_worker(executor_reference,
                             processes,
                             pending_work_items,
                             work_ids_queue,
                             call_queue,
                             result_queue):
    """Manages the communication between this process and the worker processes.
    This function is run in a local thread.
    Args:
        executor_reference: A weakref.ref to the ProcessPoolExecutor that owns
            this thread. Used to determine if the ProcessPoolExecutor has been
            garbage collected and that this function can exit.
        process: A list of the multiprocessing.Process instances used as
            workers.
        pending_work_items: A dict mapping work ids to _WorkItems e.g.
            {5: <_WorkItem...>, 6: <_WorkItem...>, ...}
        work_ids_queue: A queue.Queue of work ids e.g. Queue([5, 6, ...]).
        call_queue: A multiprocessing.Queue that will be filled with _CallItems
            derived from _WorkItems for processing by the process workers.
        result_queue: A multiprocessing.Queue of _ResultItems generated by the
            process workers.
    """
    executor = None

    def shutting_down():
        return _shutdown or executor is None or executor._shutdown_thread

    def shutdown_worker():
        # This is an upper bound
        nb_children_alive = sum(p.is_alive() for p in processes.values())
        for i in range(0, nb_children_alive):
            call_queue.put_nowait(None)
        # Release the queue's resources as soon as possible.
        call_queue.close()
        # If .join() is not called on the created processes then
        # some multiprocessing.Queue methods may deadlock on Mac OS X.
        for p in processes.values():
            p.join()

    reader = result_queue._reader

    while True:
        _add_call_item_to_queue(pending_work_items,
                                work_ids_queue,
                                call_queue)

        sentinels = [p.sentinel for p in processes.values()]
        assert sentinels
        ready = wait([reader] + sentinels)
        if reader in ready:
            result_item = reader.recv()
        else:
            # Mark the process pool broken so that submits fail right now.
            executor = executor_reference()
            if executor is not None:
                executor._broken = True
                executor._shutdown_thread = True
                executor = None
            # All futures in flight must be marked failed
            for work_id, work_item in pending_work_items.items():
                work_item.future.set_exception(
                    BrokenProcessPool(
                        "A process in the process pool was "
                        "terminated abruptly while the future was "
                        "running or pending."
                    ))
                # Delete references to object. See issue16284
                del work_item
            pending_work_items.clear()
            # Terminate remaining workers forcibly: the queues or their
            # locks may be in a dirty state and block forever.
            for p in processes.values():
                p.terminate()
            shutdown_worker()
            return
        if isinstance(result_item, int):
            # Clean shutdown of a worker using its PID
            # (avoids marking the executor broken)
            assert shutting_down()
            p = processes.pop(result_item)
            p.join()
            if not processes:
                shutdown_worker()
                return
        elif result_item is not None:
            work_item = pending_work_items.pop(result_item.work_id, None)
            # work_item can be None if another process terminated (see above)
            if work_item is not None:
                if result_item.exception:
                    work_item.future.set_exception(result_item.exception)
                else:
                    work_item.future.set_result(result_item.result)
                # Delete references to object. See issue16284
                del work_item
        # Check whether we should start shutting down.
        executor = executor_reference()
        # No more work items can be added if:
        #   - The interpreter is shutting down OR
        #   - The executor that owns this worker has been collected OR
        #   - The executor that owns this worker has been shutdown.
        if shutting_down():
            try:
                # Since no new work items can be added, it is safe to shutdown
                # this thread if there are no pending work items.
                if not pending_work_items:
                    shutdown_worker()
                    return
            except Full:
                # This is not a problem: we will eventually be woken up (in
                # result_queue.get()) and be able to send a sentinel again.
                pass
        executor = None

_system_limits_checked = False
_system_limited = None
def _check_system_limits():
    global _system_limits_checked, _system_limited
    if _system_limits_checked:
        if _system_limited:
            raise NotImplementedError(_system_limited)
    _system_limits_checked = True
    try:
        nsems_max = os.sysconf("SC_SEM_NSEMS_MAX")
    except (AttributeError, ValueError):
        # sysconf not available or setting not available
        return
    if nsems_max == -1:
        # indetermined limit, assume that limit is determined
        # by available memory only
        return
    if nsems_max >= 256:
        # minimum number of semaphores available
        # according to POSIX
        return
    _system_limited = "system provides too few semaphores (%d available, 256 necessary)" % nsems_max
    raise NotImplementedError(_system_limited)


def _chain_from_iterable_of_lists(iterable):
    """
    Specialized implementation of itertools.chain.from_iterable.
    Each item in *iterable* should be a list.  This function is
    careful not to keep references to yielded objects.
    """
    for element in iterable:
        element.reverse()
        while element:
            yield element.pop()


class BrokenProcessPool(RuntimeError):
    """
    Raised when a process in a ProcessPoolExecutor terminated abruptly
    while a future was in the running state.
    """

# class LocalGpuExecutor(futures.ProcessPoolExecutor):
#     """Wraps ProcessPoolExecutor but distributes local gpus to the
#         processes #TODO: restrict gpu allocation. At the moment restricts
#         to sequential (single core and gpu) execution."""
#     def __init__(self,max_workers,*args,**kwargs):
#         max_workers = min(max_workers,torch.cuda.device_count())
#         super().__init__(max_workers=max_workers,*args,**kwargs)
#         self._call_queue.gpu_lock = multiprocessing.Lock()
#         self._call_queue.gpus = set(range(max_workers))

class LocalGpuExecutor(_base.Executor):
    def __init__(self, max_workers=None):
        """Initializes a new ProcessPoolExecutor instance.
        Args:
            max_workers: The maximum number of processes that can be used to
                execute the given calls. If None or not given then as many
                worker processes will be created as the machine has processors.
        """
        if max_workers is None:
            max_workers = torch.cuda.device_count()#min(max_workers,torch.cuda.device_count())
        _check_system_limits()
        self._max_workers = max_workers

        # Make the call queue slightly larger than the number of processes to
        # prevent the worker processes from idling. But don't make it too big
        # because futures in the call queue cannot be cancelled.
        self._call_queue = multiprocessing.Queue(torch.cuda.device_count() +
                                                 EXTRA_QUEUED_CALLS)
        self._gpu_queue = multiprocessing.Queue(torch.cuda.device_count())
        self._possible_gpus= set(os.environ["CUDA_VISIBLE_DEVICES"].split(',')[:max_workers])
        for gpu_id in self._possible_gpus:
            self._gpu_queue.put(gpu_id)
        #self.gpu_lock = multiprocessing.Lock()
        os.environ["CUDA_DEVICE_ORDER"]="PCI_BUS_ID"
        #self.gpus = set()
        #print(self.gpus)
        #print(id(self._call_queue))
        # Killed worker processes can produce spurious "broken pipe"
        # tracebacks in the queue's own worker thread. But we detect killed
        # processes anyway, so silence the tracebacks.
        self._call_queue._ignore_epipe = True
        self._result_queue = SimpleQueue()
        self._work_ids = queue.Queue()
        self._queue_management_thread = None
        # Map of pids to processes
        self._processes = {}

        # Shutdown is a two-step process.
        self._shutdown_thread = False
        self._shutdown_lock = threading.Lock()
        self._broken = False
        self._queue_count = 0
        self._pending_work_items = {}

    def add_gpus(self,gpus):
        self._possible_gpus = self._possible_gpus.union(gpus.split(','))
        #self._max_workers = len(self._possible_gpus)
        print("New GPUs added, Pool = ",self._possible_gpus)

    def remove_gpus(self,gpus):
        gpus_to_remove = set(gpus.split(','))&self._possible_gpus
        self._possible_gpus -= gpus_to_remove
        print(f"GPUs {gpus_to_remove} removed")

    def _start_queue_management_thread(self):
        # When the executor gets lost, the weakref callback will wake up
        # the queue management thread.
        def weakref_cb(_, q=self._result_queue):
            q.put(None)
        if self._queue_management_thread is None:
            # Start the processes so that their sentinels are known.
            self._adjust_process_count()
            self._queue_management_thread = threading.Thread(
                    target=_queue_management_worker,
                    args=(weakref.ref(self, weakref_cb),
                          self._processes,
                          self._pending_work_items,
                          self._work_ids,
                          self._call_queue,
                          self._result_queue))
            self._queue_management_thread.daemon = True
            self._queue_management_thread.start()
            _threads_queues[self._queue_management_thread] = self._result_queue

    def _adjust_process_count(self):
        for _ in range(len(self._processes), self._max_workers):
            p = multiprocessing.Process(
                    target=_process_worker,
                    args=(self._call_queue,
                          self._result_queue,
                          self._possible_gpus,
                          self._gpu_queue))
            p.start()
            self._processes[p.pid] = p

    def submit(self, fn, *args, **kwargs):
        with self._shutdown_lock:
            if self._broken:
                raise BrokenProcessPool('A child process terminated '
                    'abruptly, the process pool is not usable anymore')
            if self._shutdown_thread:
                raise RuntimeError('cannot schedule new futures after shutdown')

            f = _base.Future()
            w = _WorkItem(f, fn, args, kwargs)

            self._pending_work_items[self._queue_count] = w
            self._work_ids.put(self._queue_count)
            self._queue_count += 1
            # Wake up queue management thread
            self._result_queue.put(None)

            self._start_queue_management_thread()
            return f
    submit.__doc__ = _base.Executor.submit.__doc__

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        """Returns an iterator equivalent to map(fn, iter).
        Args:
            fn: A callable that will take as many arguments as there are
                passed iterables.
            timeout: The maximum number of seconds to wait. If None, then there
                is no limit on the wait time.
            chunksize: If greater than one, the iterables will be chopped into
                chunks of size chunksize and submitted to the process pool.
                If set to one, the items in the list will be sent one at a time.
        Returns:
            An iterator equivalent to: map(func, *iterables) but the calls may
            be evaluated out-of-order.
        Raises:
            TimeoutError: If the entire result iterator could not be generated
                before the given timeout.
            Exception: If fn(*args) raises for any values.
        """
        if chunksize < 1:
            raise ValueError("chunksize must be >= 1.")

        results = super().map(partial(_process_chunk, fn),
                              _get_chunks(*iterables, chunksize=chunksize),
                              timeout=timeout)
        return _chain_from_iterable_of_lists(results)

    def shutdown(self, wait=True):
        with self._shutdown_lock:
            self._shutdown_thread = True
        if self._queue_management_thread:
            # Wake up queue management thread
            self._result_queue.put(None)
            if wait:
                self._queue_management_thread.join()
        # To reduce the risk of opening too many files, remove references to
        # objects that use file descriptors.
        self._queue_management_thread = None
        self._call_queue = None
        self._result_queue = None
        self._processes = None
    shutdown.__doc__ = _base.Executor.shutdown.__doc__




# class LocalGpuExecutor(futures.ProcessPoolExecutor):
#     """Wraps ProcessPoolExecutor but distributes local gpus to the
#         processes #TODO: restrict gpu allocation. At the moment restricts
#         to sequential (single core and gpu) execution."""
#     def __init__(self,max_workers,*args,**kwargs):
#         max_workers = min(max_workers,torch.cuda.device_count())
#         super().__init__(max_workers=max_workers,*args,**kwargs)
#         self._call_queue.gpu_lock = multiprocessing.Lock()
#         self._call_queue.gpus = set(range(max_workers))
#         #os.environ["CUDA_DEVICE_ORDER"]="PCI_BUS_ID"   # see issue #152
#         #os.environ["CUDA_VISIBLE_DEVICES"]="0"

#     def _adjust_process_count(self):
#         for _ in range(len(self._processes), self._max_workers):
#             p = multiprocessing.Process(
#                     target=_process_worker,
#                     args=(self._call_queue,
#                           self._result_queue))
#             p.start()
#             self._processes[p.pid] = p

# def _process_worker(call_queue, result_queue):
#     """Evaluates calls from call_queue and places the results in result_queue.
#     This worker is run in a separate process.
#     Args:
#         call_queue: A multiprocessing.Queue of _CallItems that will be read and
#             evaluated by the worker.
#         result_queue: A multiprocessing.Queue of _ResultItems that will written
#             to by the worker.
#         shutdown: A multiprocessing.Event that will be set as a signal to the
#             worker that it should exit when call_queue is empty.
#     """
#     while True:
#         call_item = call_queue.get(block=True)
#         if call_item is None:
#             # Wake up queue management thread
#             result_queue.put(os.getpid())
#             return
#         try:
#             call_queue.gpu_lock.acquire()
#             gpu_id = call_queue.gpus.pop()
#             call_queue.gpu_lock.release()
#             os.environ["CUDA_DEVICE_ORDER"]="PCI_BUS_ID"
#             os.environ["CUDA_VISIBLE_DEVICES"]=str(gpu_id)
#             r = call_item.fn(*call_item.args, **call_item.kwargs)
#         except BaseException as e:
#             exc = _ExceptionWithTraceback(e, e.__traceback__)
#             result_queue.put(_ResultItem(call_item.work_id, exception=exc))
#         else:
#             result_queue.put(_ResultItem(call_item.work_id,
#                                          result=r))
#         call_queue.gpu_lock.acquire()
#         call_queue.gpus.add(gpu_id)
#         call_queue.gpu_lock.release()

# class _ResultItem(object):
#     def __init__(self, work_id, exception=None, result=None):
#         self.work_id = work_id
#         self.exception = exception
#         self.result = result


# class _RemoteTraceback(Exception):
#     def __init__(self, tb):
#         self.tb = tb
#     def __str__(self):
#         return self.tb

# class _ExceptionWithTraceback:
#     def __init__(self, exc, tb):
#         tb = traceback.format_exception(type(exc), exc, tb)
#         tb = ''.join(tb)
#         self.exc = exc
#         self.tb = '\n"""\n%s"""' % tb
#     def __reduce__(self):
#         return _rebuild_exc, (self.exc, self.tb)

# def _rebuild_exc(exc, tb):
#     exc.__cause__ = _RemoteTraceback(tb)
#     return exc
# # def _process_worker(call_queue, result_queue, initializer, initargs):
# #     """Evaluates calls from call_queue and places the results in result_queue.
# #     This worker is run in a separate process.
# #     Args:
# #         call_queue: A ctx.Queue of _CallItems that will be read and
# #             evaluated by the worker.
# #         result_queue: A ctx.Queue of _ResultItems that will written
# #             to by the worker.
# #         initializer: A callable initializer, or None
# #         initargs: A tuple of args for the initializer
# #     """
# #     if initializer is not None:
# #         try:
# #             initializer(*initargs)
# #         except BaseException:
# #             _base.LOGGER.critical('Exception in initializer:', exc_info=True)
# #             # The parent will notice that the process stopped and
# #             # mark the pool broken
# #             return
# #     while True:
# #         call_item = call_queue.get(block=True)
# #         if call_item is None:
# #             # Wake up queue management thread
# #             result_queue.put(os.getpid())
# #             return
# #         try:
# #             call_queue.gpu_lock.acquire()
# #             gpu_id = call_queue.gpus.pop()
# #             call_queue.gpu_lock.release()
# #             #os.environ["CUDA_DEVICE_ORDER"]="PCI_BUS_ID"   # see issue #152
# #             os.environ["CUDA_VISIBLE_DEVICES"]=str(gpu_id)
# #             r = call_item.fn(*call_item.args, **call_item.kwargs)
# #         except BaseException as e:
# #             exc = _ExceptionWithTraceback(e, e.__traceback__)
# #             _sendback_result(result_queue, call_item.work_id, exception=exc)
# #         else:
# #             _sendback_result(result_queue, call_item.work_id, result=r)
# #             del r
# #         # Liberate the resource as soon as possible, to avoid holding onto
# #         # open files or shared memory that is not needed anymore
# #         del call_item
# #         call_queue.gpu_lock.acquire()
# #         call_queue.gpus.add(gpu_id)
# #         call_queue.gpu_lock.release()