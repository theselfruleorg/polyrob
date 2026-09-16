"""Paid-provider-free CLI replay through a real PTY and a VT screen emulator.

Run with the same Python as polyrob. Requires optional `pyte` (test-only).
Writes screen frames to /tmp/polyrob-cli-terminal-replay.json for inspection.
This verifies terminal protocol behavior, not a particular terminal app's fonts.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


async def child():
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.patch_stdout import patch_stdout
    from cli.ui import select_renderer
    from cli.ui.app import build_app
    from cli.ui.state import SessionState
    from cli.ui.events import normalize

    state = SessionState()
    state.model, state.provider, state.status = 'glm-5', 'zai-coding', 'ready'
    renderer = select_renderer(state, stream=sys.stdout, live_allowed=False)
    renderer.live_status_bar = True
    app, buf = build_app(state, on_submit=lambda _: None,
                         completer=WordCompleter(['/help', '/model', '/status']))

    async def scenario():
        await asyncio.sleep(.25)
        token = state.lifecycle.begin_turn()
        renderer.on_turn_start('хай браза')
        await asyncio.sleep(.25)
        for data in [
            {'type': 'provider_failure', 'data': {'failed_provider': 'zai-coding',
                                                'error_type': 'LLMAuthenticationError'}},
            {'type': 'provider_fallback_success', 'data': {'original_provider': 'zai-coding',
                'fallback_provider': 'gemini', 'fallback_model': 'gemini-test'}},
        ]:
            event = normalize(data)
            state.update(event)
            renderer.on_event(event)
            await asyncio.sleep(.25)
        renderer.on_turn_end('Привет, браза! Чем могу помочь?')
        state.lifecycle.end_turn(token)
        state.status = 'ready'
        await asyncio.sleep(.3)
        buf.text = 'Long input ' * 14
        buf.cursor_position = len(buf.text)
        await asyncio.sleep(.3)
        buf.text = '/'
        buf.start_completion()
        await asyncio.sleep(.3)
        buf.cancel_completion()
        buf.text = ''
        state.model = 'very-long-model-name-' * 8
        await asyncio.sleep(.3)
        state.model = 'gemini-test'
        await asyncio.sleep(.5)
        app.exit()

    with patch_stdout(raw=True):
        await app.run_async(pre_run=lambda: app.create_background_task(scenario()))


def replay():
    import fcntl
    import pty
    import select
    import signal
    import struct
    import subprocess
    import tempfile
    import termios
    import time
    import pyte

    results = {}
    with tempfile.TemporaryDirectory(prefix='polyrob-terminal-') as home:
        for color in (True, False):
            for columns, rows in ((160, 40), (80, 24), (40, 12), (20, 8)):
                master, slave = pty.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, columns, 0, 0))
                env = dict(os.environ, TERM='xterm-256color', POLYROB_HOME=home)
                env.pop('NO_COLOR', None)
                if not color:
                    env['NO_COLOR'] = '1'
                proc = subprocess.Popen([sys.executable, __file__, '--child'],
                    stdin=slave, stdout=slave, stderr=slave, env=env)
                os.close(slave)
                screen = pyte.HistoryScreen(columns, rows, history=1000)
                stream = pyte.ByteStream(screen)
                frames = []
                started = time.monotonic()
                resized = False
                try:
                    while time.monotonic() - started < 15:
                        if columns == 80 and not resized and time.monotonic() - started > 2:
                            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, 50, 0, 0))
                            screen.resize(rows, 50)
                            os.kill(proc.pid, signal.SIGWINCH)
                            resized = True
                        ready, _, _ = select.select([master], [], [], .05)
                        if ready:
                            try:
                                data = os.read(master, 65536)
                            except OSError:
                                break
                            if not data:
                                break
                            stream.feed(data)
                            if b'\x1b[6n' in data:
                                os.write(master, f'\x1b[{screen.cursor.y+1};{screen.cursor.x+1}R'.encode())
                            frames.append(screen.display[:])
                        elif proc.poll() is not None:
                            break
                finally:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait()
                    os.close(master)
                key = f'{columns}x{rows}-{"color" if color else "no-color"}'
                results[key] = {'exit': proc.returncode, 'resized': resized, 'frames': frames}
                assert proc.returncode == 0, key
                assert any(any('ready' in line for line in frame) for frame in frames), key
                assert not any('Window too small' in line for frame in frames for line in frame), key
                for frame in frames:
                    occupied = [i for i, line in enumerate(frame) if line.strip()]
                    assert all(b - a <= 2 for a, b in zip(occupied, occupied[1:])), (key, frame)
                    assert sum(line.count('хай браза') for line in frame) <= 1, (key, frame)
                if columns < 160:
                    assert any(sum('Long' in line for line in frame) > 1 for frame in frames), key
                print(f'{key}: passed ({len(frames)} frames)')
    output = Path('/tmp/polyrob-cli-terminal-replay.json')
    output.write_text(json.dumps(results, ensure_ascii=False))
    print(f'Screens: {output}')


if __name__ == '__main__':
    if '--child' in sys.argv:
        asyncio.run(child())
    else:
        replay()
