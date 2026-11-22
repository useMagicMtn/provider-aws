"""Bounded command execution over Lambda MicroVM HTTPS or EKS exec stdin."""
import json
import os
import selectors
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def validate(request):
    if not isinstance(request, dict) or set(request) != {"command", "args", "timeout_ms", "max_output_bytes"}:
        raise ValueError("invalid execution request")
    cmd, args = request["command"], request["args"]
    if not isinstance(cmd, str) or not cmd or len(cmd.encode()) > 256 or "\0" in cmd:
        raise ValueError("invalid command")
    if not isinstance(args, list) or len(args) > 32 or any(not isinstance(a, str) or len(a.encode()) > 2048 or "\0" in a for a in args):
        raise ValueError("invalid arguments")
    if type(request["timeout_ms"]) is not int or not 1 <= request["timeout_ms"] <= 30000:
        raise ValueError("invalid timeout")
    if type(request["max_output_bytes"]) is not int or not 1 <= request["max_output_bytes"] <= 16384:
        raise ValueError("invalid output limit")


def kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS can report EPERM for an already-exited process group.
        if process.poll() is None:
            raise


def execute(request, cwd="/workspace"):
    validate(request)
    start = time.monotonic()
    deadline = start + request["timeout_ms"] / 1000
    limit = request["max_output_bytes"]
    output = [bytearray(), bytearray()]
    timed_out = truncated = killed = False
    process = subprocess.Popen([request["command"], *request["args"]],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True, cwd=cwd)
    try:
        with selectors.DefaultSelector() as selector:
            for index, stream in enumerate((process.stdout, process.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, index)
            while selector.get_map():
                if time.monotonic() >= deadline and process.poll() is None:
                    timed_out = True
                    if not killed:
                        kill_group(process)
                        killed = True
                # Also close pipes inherited by background children after parent exit.
                if process.poll() is not None and not killed:
                    kill_group(process)
                    killed = True
                for key, _ in selector.select(0.01):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    remaining = limit - sum(map(len, output))
                    output[key.data].extend(chunk[:remaining])
                    truncated |= len(chunk) > remaining
                # Escaped descendants must not keep the HTTP handler alive indefinitely.
                if time.monotonic() > deadline + 1:
                    truncated = True
                    break
            process.wait(timeout=1)
    finally:
        if not killed:
            kill_group(process)
        process.wait(timeout=1)
        process.stdout.close()
        process.stderr.close()
    # Ignore a partial UTF-8 character at the byte limit instead of expanding it.
    return {"stdout": output[0].decode("utf-8", "ignore"),
            "stderr": output[1].decode("utf-8", "ignore"),
            "exit_code": process.returncode,
            "elapsed_ms": int((time.monotonic() - start) * 1000),
            "timed_out": timed_out, "truncated": truncated}


SLOTS = threading.BoundedSemaphore(4)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, code, value):
        body = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        ready = self.path in ("/health", "/aws/lambda-microvms/runtime/v1/ready", "/aws/lambda-microvms/runtime/v1/validate")
        self.respond(200 if ready else 404, {"status": "ok", "agent_version": 1} if ready else {"error": "not found"})

    def do_POST(self):
        if self.path in ("/aws/lambda-microvms/runtime/v1/ready", "/aws/lambda-microvms/runtime/v1/validate"):
            self.respond(200, {"status": "ok", "agent_version": 1})
            return
        if self.path != "/exec":
            self.respond(404, {"error": "not found"})
            return
        if not SLOTS.acquire(blocking=False):
            self.respond(429, {"error": "execution capacity reached"})
            return
        try:
            self.connection.settimeout(5)
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 128 * 1024:
                raise ValueError("invalid request size")
            request = json.loads(self.rfile.read(length))
            self.respond(200, execute(request))
        except (ValueError, KeyError, TypeError):
            self.respond(400, {"error": "invalid execution request"})
        except Exception:
            self.respond(500, {"error": "command execution failed"})
        finally:
            SLOTS.release()


if __name__ == "__main__":
    os.makedirs("/workspace", exist_ok=True)
    if sys.argv[1:] == ["--stdio"]:
        try:
            request = json.loads(sys.stdin.buffer.read(128 * 1024 + 1))
            json.dump(execute(request), sys.stdout)
        except Exception:
            sys.exit(1)
    elif sys.argv[1:] == ["--idle"]:
        while True:
            signal.pause()
    elif not sys.argv[1:]:
        ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
    else:
        sys.exit(1)
