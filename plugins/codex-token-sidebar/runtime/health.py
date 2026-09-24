"""Runtime observations, separate from content and lifecycle ownership."""
from copy import deepcopy
import time

# Three discovery/evaluation pairs (3s + 5s each), one handshake (4s), and margin.
CYCLE_GRACE = 30.0
PAGE_TIMEOUT_MS = 45000


def health_view(snapshot, deadline, now):
    result = deepcopy(snapshot)
    result["mainLoop"] = "stalled" if now > deadline else "responsive"
    if result["mainLoop"] == "stalled":
        result["state"] = "loop_stalled"
    return result


class RuntimeHealth:
    def __init__(self, *, clock=time.monotonic, wall_clock=time.time, interval=1.5):
        self.clock, self.wall_clock = clock, wall_clock
        self.timeout_ms = max(PAGE_TIMEOUT_MS, int((max(.5, interval) + CYCLE_GRACE) * 1000))
        self.deadline = clock() + CYCLE_GRACE
        self.data = {"state": "starting", "heartbeatAt": None, "lastSyncAt": None,
                     "cdp": "unknown", "target": "unknown", "mounted": False,
                     "conversation": "unknown", "reader": "unknown", "error": None}

    def begin(self):
        self.deadline = self.clock() + CYCLE_GRACE
        self.data.update(heartbeatAt=self.wall_clock(), state="checking", cdp="unknown",
                         target="unknown", mounted=False, conversation="unknown", reader="unknown")

    def page(self, state):
        self.data.update(cdp="connected", target="recognized" if state["targetRecognized"] else "unrecognized",
                         mounted=state["mounted"],
                         conversation="recognized" if state.get("conversationId") else "unknown")

    def synchronized(self, payload, reader_status):
        self.data["reader"] = reader_status
        if self.data["target"] != "recognized":
            state = "waiting_target"
        elif not self.data["mounted"]:
            state = "waiting_mount"
        elif self.data["conversation"] != "recognized":
            state = "waiting_conversation"
        elif reader_status in ("deferred", "failed"):
            state = "read_" + reader_status
        elif payload.get("status") != "ok":
            state = "waiting_data"
        else:
            state = "healthy"
            self.data["lastSyncAt"] = self.wall_clock()
        self.data["state"] = state
        self.data["error"] = ({"stage": "reader", "type": reader_status}
                              if reader_status in ("deferred", "failed") else None)

    def fail(self, stage, exc, reason=None):
        self.data.update(state="retrying", error={"stage": stage, "type": type(exc).__name__,
                                                 "reason": reason or type(exc).__name__})
        if stage == "cdp":
            self.data.update(cdp="disconnected", target="unknown", mounted=False)
        elif stage == "reader":
            self.data["reader"] = "failed"
        elif stage == "target":
            self.data.update(state="waiting_target", target="unavailable", cdp="disconnected", mounted=False)

    def finish(self, delay):
        self.data["heartbeatAt"] = self.wall_clock()
        self.deadline = self.clock() + delay + CYCLE_GRACE

    def snapshot(self):
        return health_view(self.data, self.deadline, self.clock())
