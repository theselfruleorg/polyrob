"""Bound pipe buffering while a subprocess runs (POSIX selector implementation)."""
import os
import selectors
import time


def capture(proc, *, data, timeout, limit, stop, kill):
    """Return stdout, stderr, timed_out, truncated; stop the group at the byte cap.

    Read and write incrementally, including stdin, so a child that writes before
    consuming input cannot deadlock or allocate unbounded output in the parent.
    The process belongs to the caller's dedicated process group.
    """
    output = [bytearray(), bytearray()]
    total = 0
    offset = 0
    timed_out = truncated = False
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        if limit < 1:
            raise ValueError("output limit must be positive")
        with selectors.DefaultSelector() as selector:
            for index, stream in enumerate((proc.stdout, proc.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, index)
            if proc.stdin is not None:
                if data:
                    os.set_blocking(proc.stdin.fileno(), False)
                    selector.register(proc.stdin, selectors.EVENT_WRITE, 2)
                else:
                    proc.stdin.close()
            while selector.get_map():
                if stop.is_set() or (deadline is not None and time.monotonic() >= deadline):
                    timed_out = not stop.is_set()
                    kill(proc)
                    break
                for key, _ in selector.select(timeout=0.05):
                    stream = key.fileobj
                    if key.data == 2:
                        try:
                            offset += os.write(stream.fileno(), data[offset:offset + 16384])
                        except BrokenPipeError:
                            offset = len(data)
                        if offset >= len(data):
                            selector.unregister(stream)
                            stream.close()
                        continue
                    chunk = os.read(stream.fileno(), 65536)
                    if not chunk:
                        selector.unregister(stream)
                        continue
                    room = limit - total
                    output[key.data].extend(chunk[:room])
                    total += min(room, len(chunk))
                    if len(chunk) > room:
                        truncated = True
                        kill(proc)
                        break
                if truncated:
                    break
            # A child can close its pipes before it finishes; still enforce time
            # and cancellation instead of an unbounded wait().
            while proc.poll() is None:
                if stop.is_set() or (deadline is not None and time.monotonic() >= deadline):
                    timed_out = not stop.is_set()
                    kill(proc)
                    break
                time.sleep(0.01)
        proc.wait(timeout=5)
        return bytes(output[0]), bytes(output[1]), timed_out, truncated
    finally:
        if proc.poll() is None:
            kill(proc)
            proc.wait(timeout=5)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None and not stream.closed:
                stream.close()
