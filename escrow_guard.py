# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *

import json
import typing


# Plain wallet addresses have no GenLayer code, so payouts go through an
# EVM-style interface instead of treating the recipient as a GenLayer contract.
@gl.evm.contract_interface
class _Recipient:
    class View:
        pass

    class Write:
        pass


class EscrowGuard(gl.Contract):
    """
    Freelance escrow that holds real GEN and settles disputes with
    validator consensus.

    Stages:
      AWAITING_FUNDS -> FUNDED -> DELIVERED -> SETTLED
                                     |
                                     +-> DISPUTED -> SETTLED

    1. The client funds the escrow (payable).
    2. The freelancer submits a delivery note and a link to the work.
    3. The client either approves (funds released to the freelancer) or
       raises a dispute.
    4. On dispute, validators read the agreed terms, the delivery note,
       the live content of the delivery link, and the client's complaint,
       then agree on a ruling. The escrow is paid out automatically.
    """

    client: Address
    freelancer: Address
    job_terms: str
    stage: str
    amount: u256
    delivery_note: str
    delivery_url: str
    complaint: str
    ruling: str
    reasoning: str

    # NOTE: gl.message.sender_address is not used in __init__ (it can break
    # deployment), so the client address is passed in as a parameter.
    def __init__(self, client: str, freelancer: str, job_terms: str):
        self.client = Address(client)
        self.freelancer = Address(freelancer)
        self.job_terms = job_terms
        self.stage = "AWAITING_FUNDS"
        self.amount = u256(0)
        self.delivery_note = ""
        self.delivery_url = ""
        self.complaint = ""
        self.ruling = ""
        self.reasoning = ""

    # ---------- small helpers ----------

    def _only(self, who: Address, label: str) -> None:
        if gl.message.sender_address.as_hex != who.as_hex:
            raise gl.vm.UserError(f"[EXPECTED] Only the {label} can do this")

    def _in_stage(self, expected: str) -> None:
        if self.stage != expected:
            raise gl.vm.UserError(
                f"[EXPECTED] Wrong stage: contract is {self.stage}, needs {expected}"
            )

    def _pay(self, to: Address) -> None:
        # State is already marked SETTLED before this is called.
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
        self.stage = "FUNDED"
        return "Escrow funded"

    # ---------- stage 2: delivery ----------

    @gl.public.write
    def submit_delivery(self, delivery_note: str, delivery_url: str) -> str:
        self._only(self.freelancer, "freelancer")
        self._in_stage("FUNDED")
        self.delivery_note = delivery_note
        self.delivery_url = delivery_url
        self.stage = "DELIVERED"
        return "Delivery submitted"

    # ---------- stage 3a: happy path ----------

    @gl.public.write
    def approve_release(self) -> str:
        self._only(self.client, "client")
        self._in_stage("DELIVERED")
        self.ruling = "PAY_FREELANCER"
        self.reasoning = "Client approved the delivery."
        self.stage = "SETTLED"
        self._pay(self.freelancer)
        return "Funds released to freelancer"

    # ---------- stage 3b: dispute ----------

    @gl.public.write
    def raise_dispute(self, complaint: str) -> str:
        self._only(self.client, "client")
        self._in_stage("DELIVERED")
        self.complaint = complaint
        self.stage = "DISPUTED"
        return "Dispute raised"

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
            "stage": self.stage,
            "amount": str(int(self.amount)),
            "delivery_note": self.delivery_note,
            "delivery_url": self.delivery_url,
            "complaint": self.complaint,
            "ruling": self.ruling,
            "reasoning": self.reasoning,
        }
