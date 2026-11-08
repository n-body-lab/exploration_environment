"""The observer page in a real browser (headless Chrome over the DevTools protocol).

Checks what is actually drawn for a finished scripted run: node balances and their change,
the backchannel feed, counters, and that the page stays put. Skipped without Chrome.
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
import urllib.request
from pathlib import Path

import pytest
import uvicorn

from hivemarket.analysis.pipeline import analyze
from hivemarket.orchestrator.local import run_local
from hivemarket.orchestrator.run import create_run
from hivemarket.ui.server import create_app

websockets = pytest.importorskip("websockets")

ROOT = Path(__file__).resolve().parents[1]
CHROME = next((p for p in [os.environ.get("HM_CHROME"),
                           "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                           shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chromium-browser")]
               if p and Path(p).exists()), None)
pytestmark = pytest.mark.skipif(CHROME is None, reason="Chrome not installed")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Page:
    """Just enough of the DevTools protocol: navigate and evaluate."""

    def __init__(self, ws):
        self.ws, self.n = ws, 0

    async def call(self, method, **params):
        self.n += 1
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            m = json.loads(await self.ws.recv())
            if m.get("id") == self.n:
                return m.get("result", {})

    async def js(self, expression):
        r = await self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")

    async def wait_for(self, expression, timeout=15.0):
        for _ in range(int(timeout / 0.2)):
            if await self.js(expression):
                return
            await asyncio.sleep(0.2)
        raise AssertionError(f"timed out waiting for {expression}")


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory):
    runs = tmp_path_factory.mktemp("runs")
    run_dir = create_run(ROOT / "experiments" / "local_fail.yaml", runs, mode="local", local_base_port=19900)
    asyncio.run(run_local(run_dir))
    analyze(run_dir, embeddings="hashing")
    return run_dir


async def test_page_draws_a_finished_run(run_dir):
    port, devtools = _free_port(), _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(run_dir.parent), host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    chrome = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={devtools}",
                               f"--user-data-dir={tempfile.mkdtemp()}", "--window-size=1400,1100", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        while not server.started:
            await asyncio.sleep(0.05)
        for _ in range(100):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{devtools}/json"))
                ws_url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")
                break
            except Exception:
                await asyncio.sleep(0.1)
        async with websockets.connect(ws_url, max_size=20_000_000) as ws:
            page = Page(ws)
            await page.call("Runtime.enable")
            await page.call("Page.navigate", url=f"http://127.0.0.1:{port}/#{run_dir.name}")
            await page.wait_for("document.getElementById('counts').textContent.includes('backchannel alerts')")
            await asyncio.sleep(0.6)  # one more redraw tick

            assert await page.js("document.getElementById('error').hidden"), await page.js(
                "document.getElementById('error').textContent")
            # The feed lists the alerts the counter counts (regression: it once always said "none seen").
            alerts = await page.js("[...document.querySelectorAll('#alerts li:not(.empty)')].map(li => li.textContent)")
            assert alerts and all("agent-3" in a and "agent-1" in a for a in alerts), alerts
            assert not await page.js("document.querySelector('#alerts li.empty')")
            assert not await page.js("document.getElementById('back-first').hidden")
            assert await page.js("!document.getElementById('banner').hidden")

            # Each node shows its balance and the change since the round started.
            final = json.loads((run_dir / "final_state.json").read_text())["credits"]
            start = {n: spec["credits"] for n, spec in json.loads((run_dir / "world.json").read_text())["nodes"].items()}
            texts = await page.js("[...document.querySelectorAll('#graph .g-credits')].map(t => "
                                  "[...t.childNodes].filter(n => n.nodeName !== 'title').map(n => n.textContent).join(''))")
            for agent, credits in final.items():
                change = credits - start[agent]
                expected = f"{credits} cr" + (f"  {'+' if change > 0 else ''}{change}" if change else "")
                assert expected in texts, (agent, expected, texts)
            assert final["agent-1"] > start["agent-1"] and final["agent-2"] < start["agent-2"]  # a sale happened

            # The market's take: the last round's burned credits, and the whole run's (the round total
            # reset when round 2 started, so it is less than the run total).
            rows = [json.loads(line) for line in (run_dir / "logs" / "market.jsonl").read_text().splitlines()]
            ends = [r["data"] for r in rows if r["kind"] == "round_end"]  # not the notices announcing it
            burned = [e["credits_check"]["burned"] for e in ends]
            assert len(burned) >= 2 and burned[-1] < sum(burned)  # so the round total did reset
            box = await page.js("[...document.querySelectorAll('#market-take text')].map(t => t.textContent)")
            assert box and box[0].startswith("Market's take"), box
            assert f"this round {burned[-1]} cr" in box, box
            assert any(t.startswith(f"this run {sum(burned)} cr") for t in box), box

            # The event stream fills its own box; the page itself doesn't scroll or grow while it redraws.
            assert await page.js("document.querySelectorAll('#log tr[data-id]').length") > 20
            before = await page.js("[scrollY, document.documentElement.scrollHeight]")
            await page.js("document.getElementById('hide-noise').click()")  # many more rows
            await asyncio.sleep(0.6)
            assert await page.js("[scrollY, document.documentElement.scrollHeight]") == before
    finally:
        chrome.terminate()
        server.should_exit = True
        await task
