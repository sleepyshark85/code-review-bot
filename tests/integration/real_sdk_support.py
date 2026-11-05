"""What the real-SDK tests share: a fake CLI that is a real child process, and its checks."""

from fake_sdk import HIGH_FINDING, findings_json
from p0_s4_support import assert_one_error_line

FINDING_LINE_START = "src/app.py:7"

FAKE_CLI = """
import json, pathlib, sys, time

marker, mode = sys.argv[1:]

def send(message):
    print(json.dumps(message), flush=True)

def assistant(error, text):
    message = {"model": "m", "content": [{"type": "text", "text": text}]}
    send({"type": "assistant", "error": error, "message": message, "session_id": "s"})

def result(**fields):
    send({
        "type": "result", "subtype": "success", "duration_ms": 1, "duration_api_ms": 1,
        "num_turns": 1, "session_id": "s", "usage": {"input_tokens": 2, "output_tokens": 7},
        **fields,
    })

def answer_initialize_then_read_the_prompt():
    request = json.loads(sys.stdin.readline())
    send({"type": "control_response", "response": {
        "subtype": "success", "request_id": request["request_id"], "response": {},
    }})
    sys.stdin.readline()

pathlib.Path(marker).write_text("ran", encoding="utf-8")
sys.stderr.write("SENTINEL from the CLI test-key-sentinel\\n")
sys.stderr.flush()

GOOD = GOOD_LITERAL
FAILED_401 = dict(
    is_error=True, stop_reason="stop_sequence", terminal_reason="api_error",
    api_error_status=401, result="SENTINEL Invalid API key",
)

if mode == "crash_on_the_first_request":
    sys.stdin.readline()
    sys.exit(3)
if mode == "malformed_json":
    sys.stdin.readline()
    print('{"type": SENTINEL is not JSON', flush=True)
    sys.exit(0)

answer_initialize_then_read_the_prompt()

if mode == "live_401":
    assistant("authentication_failed", "SENTINEL Invalid API key")
    result(**FAILED_401)
    sys.exit(1)
if mode == "result_401_then_crash":
    result(**FAILED_401)
    sys.exit(1)
if mode == "crash_after_the_prompt":
    sys.exit(3)
if mode == "malformed_after_the_prompt":
    print('{"type": SENTINEL is not JSON', flush=True)
    sys.exit(0)
if mode == "success_then_crash":
    result(is_error=False, result=GOOD)
    sys.exit(3)
if mode == "signal_then_success":
    assistant("rate_limit", "SENTINEL slow down")
    result(is_error=False, result=GOOD)
    sys.exit(0)
if mode == "prose":
    result(is_error=False, result="SENTINEL prose")
    sys.exit(0)
if mode == "success_then_never_ends":
    result(is_error=False, result=GOOD)
    time.sleep(45)
if mode == "never_ends":
    time.sleep(45)
sys.exit(0)
""".replace("GOOD_LITERAL", repr(findings_json(HIGH_FINDING)))


def assert_failed_cleanly(completed, code):
    assert completed.returncode == code, completed.stderr
    assert completed.stdout == ""
    assert "SENTINEL" not in completed.stderr
    assert "Fatal error" not in completed.stderr
    assert_one_error_line(completed.stderr)
