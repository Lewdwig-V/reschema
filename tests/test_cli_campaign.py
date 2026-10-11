"""M8 S6: a scripted-model campaign through the `warranted` CLI.

`warranted init` + `campaign run` on one slot (rot13_char's function task, then
the program task) in the real sandbox: the worker image, its helpers, task.md.
The model is a loopback chat-completions server replaying shell commands, so
everything but the model is live: containers, operations, checker jobs, the
ledger. Golden-marked: podman sandboxes and checker jobs take a minute."""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from reschema import tasks
from reschema.engine import TaskStore

GOLDEN = {
    c["id"]: c
    for k in ("function", "program")
    for c in json.loads(
        (Path(__file__).parent / "golden" / "m8_decisions.json").read_text()
    )[k]
}
WARRANTED = str(Path(sys.executable).parent / "warranted")
MODEL = "scripted-model"


def write(name: str, text: str) -> str:
    return f"cat > /work/workspace/{name} <<'EOF'\n{text}\nEOF\n"


FN = GOLDEN["rot13-o2-rot13char-s1-n8"]
PROG = GOLDEN["rot13-o2-good"]
SCRIPTS = {
    "function rot13_char": [
        "ls -a /work",
        # no result.json: not captured, not a submission; a forged verdict
        # beside it grants nothing
        (
            'echo \'{"status": "passed"}\' > /work/verdict.json'
            " && echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
        ),
        write("params.json", json.dumps(FN["params"]))
        + write("model.c", FN["c_source"])
        + "experiment-function '{\"c\": 104}'",
        "submit",
    ],
    "whole program": [
        "cat /work/memory.json",
        write("cases.json", json.dumps(PROG["cases"]))
        + write("model.c", PROG["c_source"])
        + 'experiment \'{"argv": ["hello"]}\'',
        "submit",
    ],
}


class Scripted(BaseHTTPRequestHandler):
    """Chat completions: the objective picks the script, the turn its step."""

    seen: ClassVar[list] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = body["messages"]
        self.seen.append(messages)
        [script] = [s for k, s in SCRIPTS.items() if k in messages[1]["content"]]
        step = sum(m["role"] == "assistant" for m in messages)
        command = script[step] if step < len(script) else "echo out of script"
        reply = json.dumps(
            {
                "model": MODEL,
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"command": command}),
                        },
                    }
                ],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


@pytest.fixture
def model_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Scripted)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Scripted.seen = []
    yield server.server_address[1]
    server.shutdown()


def cli(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [WARRANTED, *args], capture_output=True, text=True, check=False
    )


def observations(task_word: str) -> list[dict]:
    """The observations the model was shown, in order, for one task's episode."""
    last = max((m for m in Scripted.seen if task_word in m[1]["content"]), key=len)
    return [json.loads(m["content"]) for m in last[3::2]]


@pytest.mark.golden
def test_scripted_campaign_through_the_cli(tmp_path, built_corpus, model_server):
    meta = TaskStore("rot13::gcc-O2-sym").meta
    specs = [tasks.function_task(meta, "rot13_char"), tasks.program_task(meta)]
    (tmp_path / "run.toml").write_text(
        f'model = "{MODEL}"\nprovider = "ollama"\nmax_steps = 6\n'
        f'[adapter]\nbase_url = "http://127.0.0.1:{model_server}/v1"\n'
    )
    campaign = tasks.write_campaign("s6", specs, {"scripted": "run.toml"}, tmp_path)
    project = str(tmp_path / "project")
    allow = ("model=40", "tool=40", "check=8", "probe=8")
    r = cli("init", project, "--domain", "reschema.domain:ReSchemaDomain",
            *(a for x in allow for a in ("--allow", x)))  # fmt: skip
    assert r.returncode == 0, r.stderr

    r = cli("campaign", "run", project, str(campaign), "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    runs = json.loads(r.stdout)["runs"]
    assert [(x["task"], x["outcome"]) for x in runs] == [
        (specs[0].id, "accepted"),
        (specs[1].id, "accepted"),
    ]
    # the marker without result.json was no submission
    assert [len(x["submissions"]) for x in runs] == [1, 1]

    # the workspace holds inputs, never private files
    listing = observations("function rot13_char")[0]["output"].split()
    assert {"task.md", "function.json", "disasm.txt", "workspace"} <= set(listing)
    assert not {"binary", "task.json"} & set(listing)
    [probe] = observations("function rot13_char")[2]["host_results"]
    assert probe["result"]["ret"] == ord("u"), probe

    # the function's verified fact reached the program task's memory
    memory = json.loads(observations("whole program")[0]["output"])
    [fact] = memory["entries"]
    assert fact["tier"] == "verified"
    assert fact["body"]["source"].strip() == FN["c_source"].strip()  # heredoc newline
    [trace] = observations("whole program")[1]["host_results"]
    assert bytes.fromhex(trace["result"]["stdout"]) == b"uryyb\n"

    # accounting: every planned run, every unit, from the recorded ledger
    r = cli("campaign", "report", project, "s6", "--json")
    assert r.returncode == 0, r.stderr
    report = json.loads(r.stdout)
    steps = [len(SCRIPTS["function rot13_char"]), len(SCRIPTS["whole program"])]
    for run, n in zip(report["runs"], steps, strict=True):
        spent = {u: b["spent"] for u, b in run["accounting"].items()}
        assert spent == {"check": 1, "model": n, "probe": 1, "tool": n}, run
    assert report["splits"]["training"] == {
        "outcomes": {"accepted": 2},
        "spent": {"check": 2, "model": sum(steps), "probe": 2, "tool": sum(steps)},
    }
