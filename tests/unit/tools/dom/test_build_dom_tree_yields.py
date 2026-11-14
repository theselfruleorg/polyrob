"""_build_dom_tree parses a big node map in pure Python on the event loop.

On a heavy page that blocked the loop for 2+ s at a time (`event loop stuck`
in prod). It must hand control back to the loop while it works.
"""
import asyncio

from tools.dom.service import DomService

N = 1000


class _FakePage:
    async def evaluate(self, code, args=None):
        if code == "1+1":
            return 2
        if args is None:
            return {"success": True}
        nodes = {
            str(i): {"tagName": "DIV", "xpath": f"/div[{i}]", "children": [], "isVisible": True}
            for i in range(N)
        }
        nodes["0"]["children"] = [str(i) for i in range(1, N)]
        return {"map": nodes, "rootId": "0"}


def test_build_dom_tree_yields_to_the_loop():
    async def run():
        svc = DomService(_FakePage())
        ticks = 0
        stop = False

        async def ticker():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0)

        t = asyncio.create_task(ticker())
        tree, _ = await svc._build_dom_tree(False, -1, 0)
        stop = True
        await t
        return tree, ticks

    tree, ticks = asyncio.run(run())
    assert len(tree.children) == N - 1  # result is unchanged
    assert ticks >= 10, f"parse never yielded to the event loop (ticks={ticks})"
