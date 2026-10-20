"""Tests for EscrowGuard's deadline and recovery logic. GenLayer's runtime is
stubbed, so these check the plain-code rules, not live consensus.
Run with:  python test_escrow_guard.py"""
import importlib.util, pathlib, sys, types

HERE = pathlib.Path(__file__).parent
CLIENT, FREELANCER, OUTSIDER = "0xC1", "0xF1", "0xX1"

class Addr(str):
    @property
    def as_hex(self): return str(self)

class UserError(Exception): pass

clock = {"now": 1_000_000}
sender = {"who": CLIENT}
value = {"amt": 0}
sent = []

def write(fn): return fn
write.payable = write

gl = types.SimpleNamespace(
    Contract=object,
    public=types.SimpleNamespace(write=write, view=lambda fn: fn),
    evm=types.SimpleNamespace(contract_interface=lambda cls: cls),
    vm=types.SimpleNamespace(UserError=UserError, Return=type("Return", (), {})),
    nondet=types.SimpleNamespace(web=None, exec_prompt=None),
    eq_principle=types.SimpleNamespace(prompt_comparative=None),
    message_raw={"datetime": "1970-01-01T00:00:00.000000Z"},
    message=types.SimpleNamespace(sender_address=None, value=0),
)
mod = types.ModuleType("genlayer")
mod.gl, mod.u256, mod.Address = gl, int, Addr
sys.modules["genlayer"] = mod

spec = importlib.util.spec_from_file_location("eg", HERE / "escrow_guard.py")
eg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eg)

class FakeRecipient:
    def __init__(self, to): self.to = to
    def emit_transfer(self, value): sent.append((self.to, int(value)))
eg._Recipient = FakeRecipient

def _iso(epoch):
    import datetime as _dt
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")

def test_now_matches_python_datetime():
    import datetime as _dt
    for epoch in [0, 951782400, 1_000_000, 1_789_000_000, 4_102_444_800]:
        gl.message_raw = {"datetime": _iso(epoch)}
        assert eg._now() == epoch, (epoch, eg._now())
    gl.message_raw = {"datetime": "2026-09-26T20:06:34.271991Z"}
    assert eg._now() == int(_dt.datetime(2026, 9, 26, 20, 6, 34, tzinfo=_dt.timezone.utc).timestamp())

def as_(who, amt=0):
    sender["who"] = who
    value["amt"] = amt
    gl.message.sender_address = Addr(who)
    gl.message.value = amt
    gl.message_raw = {"datetime": _iso(clock["now"])}

def fresh(window=3600):
    sent.clear()
    clock["now"] = 1_000_000
    return eg.EscrowGuard(CLIENT, FREELANCER, "Build a page", window)

def funded(window=3600):
    c = fresh(window)
    as_(CLIENT, 1000)
    c.fund_escrow()
    return c

def delivered(window=3600):
    c = funded(window)
    as_(FREELANCER)
    c.submit_delivery("done", "https://example.com")
    return c

def raises(fn, fragment=""):
    try:
        fn()
    except UserError as e:
        assert fragment in str(e), str(e)
        return
    raise AssertionError("expected UserError containing " + repr(fragment))

def test_deadlines_are_set_from_the_contract_clock():
    c = funded(3600)
    assert int(c.delivery_deadline) == 1_000_000 + 3600
    clock["now"] = 1_000_500
    as_(FREELANCER)
    c.submit_delivery("done", "https://example.com")
    assert int(c.response_deadline) == 1_000_500 + 3600
    assert c.stage == "DELIVERED"

def test_constructor_limits_the_window():
    raises(lambda: eg.EscrowGuard(CLIENT, FREELANCER, "x", 10), "window_seconds")
    raises(lambda: eg.EscrowGuard(CLIENT, FREELANCER, "x", 59), "window_seconds")
    raises(lambda: eg.EscrowGuard(CLIENT, FREELANCER, "x", 99_999_999), "window_seconds")

def test_client_reclaims_after_missed_delivery():
    c = funded(3600)
    as_(CLIENT); raises(lambda: c.reclaim_undelivered(), "still open")
    clock["now"] = 1_000_000 + 3600 + 1
    as_(FREELANCER); raises(lambda: c.reclaim_undelivered(), "Only the client")
    as_(CLIENT); c.reclaim_undelivered()
    assert c.stage == "REFUNDED" and sent == [(CLIENT, 1000)]

def test_late_delivery_is_refused():
    c = funded(3600)
    clock["now"] = 1_000_000 + 3600 + 1
    as_(FREELANCER); raises(lambda: c.submit_delivery("late", "https://example.com"), "delivery window")
    assert c.stage == "FUNDED"

def test_freelancer_claims_when_client_is_silent():
    c = delivered(3600)
    as_(FREELANCER); raises(lambda: c.claim_unanswered(), "still open")
    clock["now"] = c.response_deadline + 1
    as_(CLIENT); raises(lambda: c.raise_dispute("too late"), "response window has passed")
    as_(FREELANCER); c.claim_unanswered()
    assert c.stage == "SETTLED" and sent == [(FREELANCER, 1000)]

def test_client_can_still_respond_inside_the_window():
    c = delivered(3600)
    clock["now"] = 1_000_000 + 3600 + 100
    as_(CLIENT); c.approve_release()
    assert c.stage == "SETTLED" and sent == [(FREELANCER, 1000)]

def test_no_double_settlement():
    c = delivered(3600)
    clock["now"] = 1_000_000 + 3600 + 3600 + 1
    as_(FREELANCER); c.claim_unanswered()
    as_(FREELANCER); raises(lambda: c.claim_unanswered(), "Wrong stage")
    as_(CLIENT); raises(lambda: c.reclaim_undelivered(), "Wrong stage")
    assert sent == [(FREELANCER, 1000)]

def test_stalled_dispute_returns_money_after_deadline():
    c = delivered(3600)
    as_(CLIENT); c.raise_dispute("bad form")
    assert int(c.dispute_deadline) == clock["now"] + 3600
    as_(FREELANCER); raises(lambda: c.settle_stalled_dispute(), "still have time")
    clock["now"] = int(c.dispute_deadline) + 1
    as_(OUTSIDER); raises(lambda: c.settle_stalled_dispute(), "Only the client or freelancer")
    as_(FREELANCER); c.settle_stalled_dispute()
    assert c.stage == "SETTLED" and c.ruling == "REFUND_CLIENT" and sent == [(CLIENT, 1000)]
    as_(CLIENT); raises(lambda: c.settle_stalled_dispute(), "Wrong stage")
    assert sent == [(CLIENT, 1000)]

def test_refund_cannot_follow_delivery():
    c = delivered(3600)
    clock["now"] = 1_000_000 + 3600 + 1
    as_(CLIENT); raises(lambda: c.reclaim_undelivered(), "Wrong stage")

def test_outsider_cannot_touch_anything():
    c = funded(3600)
    clock["now"] = 1_000_000 + 9999
    as_(OUTSIDER)
    raises(lambda: c.reclaim_undelivered(), "Only the client")
    raises(lambda: c.submit_delivery("x", "https://e.com"), "Only the freelancer")

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t(); print("PASS", t.__name__)
    print(len(tests), "tests passed")
