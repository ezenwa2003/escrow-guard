# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *

import json
import typing

MIN_WINDOW = 60             # 1 minute (use days in production)
MAX_WINDOW = 2_592_000      # 30 days


# Plain wallets have no GenLayer code, so payouts use an EVM-style interface.
@gl.evm.contract_interface
class _Recipient:
    class View:
        pass

    class Write:
        pass


def _now() -> int:
    return int(gl.block.timestamp)


class EscrowGuard(gl.Contract):
    """
    Freelance escrow that holds real GEN. Nothing stays locked forever.

    Stages:
      AWAITING_FUNDS -> FUNDED -> DELIVERED -> SETTLED
                          |            |
                          |            +-> DISPUTED -> SETTLED
                          |            |
                          |            +-> (client silent past response window)
                          |                 freelancer calls claim_unanswered -> SETTLED
                          +-> (freelancer silent past delivery window)
                               client calls reclaim_undelivered -> REFUNDED

    Every deadline is set from the contract's own clock when the step happens.
    Every transition checks the caller's role and the current stage before
    money moves, and the stage is written before any transfer, so a payout
    can only happen once.
    """

    client: Address
    freelancer: Address
    job_terms: str
    window_seconds: u256
    stage: str
    amount: u256
    delivery_deadline: u256
    response_deadline: u256
    dispute_deadline: u256
    delivery_note: str
    delivery_url: str
    complaint: str
    ruling: str
    reasoning: str

    def __init__(self, client: str, freelancer: str, job_terms: str, window_seconds: int):
        if not MIN_WINDOW <= window_seconds <= MAX_WINDOW:
            raise gl.vm.UserError("[EXPECTED] window_seconds must be between 3600 and 2592000")
        self.client = Address(client)
        self.freelancer = Address(freelancer)
        self.job_terms = job_terms
        self.window_seconds = u256(window_seconds)
        self.stage = "AWAITING_FUNDS"
        self.amount = u256(0)
        self.delivery_deadline = u256(0)
        self.response_deadline = u256(0)
        self.dispute_deadline = u256(0)
        self.delivery_note = ""
        self.delivery_url = ""
        self.complaint = ""
        self.ruling = ""
        self.reasoning = ""

    # ---------- helpers ----------

    def _only(self, who: Address, label: str) -> None:
        if gl.message.sender_address.as_hex != who.as_hex:
            raise gl.vm.UserError(f"[EXPECTED] Only the {label} can do this")

    def _in_stage(self, expected: str) -> None:
        if self.stage != expected:
            raise gl.vm.UserError(
                f"[EXPECTED] Wrong stage: contract is {self.stage}, needs {expected}"
            )

    def _pay(self, to: Address) -> None:
        _Recipient(to).emit_transfer(value=self.amount)

    # ---------- stage 1: funding ----------

    @gl.public.write.payable
    def fund_escrow(self) -> str:
        self._only(self.client, "client")
        self._in_stage("AWAITING_FUNDS")
        value = gl.message.value
        if value == 0:
            raise gl.vm.UserError("[EXPECTED] Attach a non-zero amount of GEN")
        self.amount = value
        self.delivery_deadline = u256(_now() + int(self.window_seconds))
        self.stage = "FUNDED"
        return "Escrow funded"

    # ---------- stage 2: delivery ----------

    @gl.public.write
    def submit_delivery(self, delivery_note: str, delivery_url: str) -> str:
        self._only(self.freelancer, "freelancer")
        self._in_stage("FUNDED")
        if _now() > int(self.delivery_deadline):
            raise gl.vm.UserError("[EXPECTED] The delivery window has passed")
        if not delivery_note.strip():
            raise gl.vm.UserError("[EXPECTED] Describe what you delivered")
        self.delivery_note = delivery_note
        self.delivery_url = delivery_url
        self.response_deadline = u256(_now() + int(self.window_seconds))
        self.stage = "DELIVERED"
        return "Delivery submitted"

    # ---------- stage 3: client responds ----------

    @gl.public.write
    def approve_release(self) -> str:
        self._only(self.client, "client")
        self._in_stage("DELIVERED")
        self.ruling = "PAY_FREELANCER"
        self.reasoning = "Client approved the delivery."
        self.stage = "SETTLED"
        self._pay(self.freelancer)
        return "Funds released to freelancer"

    @gl.public.write
    def raise_dispute(self, complaint: str) -> str:
        self._only(self.client, "client")
        self._in_stage("DELIVERED")
        if _now() > int(self.response_deadline):
            raise gl.vm.UserError("[EXPECTED] The response window has passed. The freelancer can now claim payment.")
        if not complaint.strip():
            raise gl.vm.UserError("[EXPECTED] Say what is wrong with the delivery")
        self.complaint = complaint
        self.dispute_deadline = u256(_now() + int(self.window_seconds))
        self.stage = "DISPUTED"
        return "Dispute raised"

    # ---------- recovery paths ----------

    @gl.public.write
    def reclaim_undelivered(self) -> str:
        """Client gets the money back when the freelancer never delivered in time."""
        self._only(self.client, "client")
        self._in_stage("FUNDED")
        if _now() <= int(self.delivery_deadline):
            raise gl.vm.UserError("[EXPECTED] The delivery window is still open")
        self.ruling = "REFUND_CLIENT"
        self.reasoning = "No delivery was submitted before the deadline."
        self.stage = "REFUNDED"
        self._pay(self.client)
        return "Refunded to client"

    @gl.public.write
    def claim_unanswered(self) -> str:
        """Freelancer gets paid when the client never approved or disputed in time."""
        self._only(self.freelancer, "freelancer")
        self._in_stage("DELIVERED")
        if _now() <= int(self.response_deadline):
            raise gl.vm.UserError("[EXPECTED] The client's response window is still open")
        self.ruling = "PAY_FREELANCER"
        self.reasoning = "The client did not approve or dispute before the deadline."
        self.stage = "SETTLED"
        self._pay(self.freelancer)
        return "Funds released to freelancer"

    # ---------- stage 4: validators rule on a dispute ----------

    @gl.public.write
    def settle_stalled_dispute(self) -> str:
        """Safety valve: if validators have not ruled by the dispute deadline,
        either party can close the case and the escrow returns to the client.
        Without this, a dispute the validators can never agree on would lock
        the money forever."""
        caller = gl.message.sender_address.as_hex
        if caller != self.client.as_hex and caller != self.freelancer.as_hex:
            raise gl.vm.UserError("[EXPECTED] Only the client or freelancer can do this")
        self._in_stage("DISPUTED")
        if _now() <= int(self.dispute_deadline):
            raise gl.vm.UserError("[EXPECTED] The validators still have time to rule")
        self.ruling = "REFUND_CLIENT"
        self.reasoning = "Validators did not reach a ruling before the deadline, so the escrow returned to the client."
        self.stage = "SETTLED"
        self._pay(self.client)
        return "Escrow returned to client"

    @gl.public.write
    def resolve_dispute(self) -> typing.Any:
        self._in_stage("DISPUTED")
        caller = gl.message.sender_address.as_hex
        if caller != self.client.as_hex and caller != self.freelancer.as_hex:
            raise gl.vm.UserError("[EXPECTED] Only the client or freelancer can do this")

        terms = self.job_terms
        note = self.delivery_note
        url = self.delivery_url
        complaint = self.complaint

        def nondet() -> str:
            work_content = "(no delivery link provided)"
            if url:
                try:
                    response = gl.nondet.web.get(url)
                    work_content = response.body.decode("utf-8")[:6000]
                except Exception:
                    work_content = "(the delivery link could not be fetched)"

            task = f"""
You are a neutral arbiter settling a freelance escrow dispute.

Agreed job terms:
{terms}

Freelancer's delivery note:
{note}

Live content found at the freelancer's delivery link:
{work_content}

Client's complaint:
{complaint}

Work through the agreed terms one requirement at a time. For each, decide
whether the delivery note and the delivery link content show it was met.
Then decide whether the client's complaint points to a real unmet requirement.

Respond using ONLY the following JSON format:
{{
    "ruling": str,     // exactly "PAY_FREELANCER" or "REFUND_CLIENT"
    "reasoning": str   // one or two short sentences explaining the ruling
}}
It is mandatory that you respond only using the JSON format above,
nothing else. Don't include any other words or characters,
your output must be only JSON without any formatting prefix or suffix.
This result should be perfectly parsable by a JSON parser without errors.
"""
            result = (
                gl.nondet.exec_prompt(task)
                .replace("```json", "")
                .replace("```", "")
            )
            return json.dumps(json.loads(result), sort_keys=True)

        result_str = gl.eq_principle.prompt_comparative(
            nondet,
            principle=(
                "The 'ruling' field must be the same value (PAY_FREELANCER "
                "or REFUND_CLIENT). The 'reasoning' field may be worded "
                "differently as long as it justifies the same ruling based "
                "on the terms, delivery and complaint."
            ),
        )
        result_json = json.loads(result_str)
        ruling = result_json["ruling"]
        if ruling not in ("PAY_FREELANCER", "REFUND_CLIENT"):
            raise gl.vm.UserError("[EXPECTED] Validators returned an invalid ruling")

        self.ruling = ruling
        self.reasoning = result_json["reasoning"]
        self.stage = "SETTLED"

        if ruling == "PAY_FREELANCER":
            self._pay(self.freelancer)
        else:
            self._pay(self.client)

        return result_json

    # ---------- read-only ----------

    @gl.public.view
    def get_escrow(self) -> dict:
        return {
            "client": self.client.as_hex,
            "freelancer": self.freelancer.as_hex,
            "job_terms": self.job_terms,
            "window_seconds": str(int(self.window_seconds)),
            "stage": self.stage,
            "amount": str(int(self.amount)),
            "delivery_deadline": str(int(self.delivery_deadline)),
            "response_deadline": str(int(self.response_deadline)),
            "dispute_deadline": str(int(self.dispute_deadline)),
            "delivery_note": self.delivery_note,
            "delivery_url": self.delivery_url,
            "complaint": self.complaint,
            "ruling": self.ruling,
            "reasoning": self.reasoning,
        }
