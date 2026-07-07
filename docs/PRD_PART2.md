# Nova Part 2 - PRD: CG Verification Agent

**One line:** when SU's email lands, the agent has already read every attachment,
checked every field against the customer's rules *and across documents*, and put a
ready-to-send reply in CG's queue. CG reviews and clicks send. Nothing changes about
who does what - SU sends, CG validates, the customer receives - only the reading and
typing disappear.

---

## Personas

**Priya - CG validator (8 yrs in ops, not technical).** Works an inbox of supplier
document sets all day. Rules for each customer live in her head; she is the reason a
wrong HS code doesn't reach customs. She will not trust a black box: before anything
goes out under her name she wants to see *what the agent found, where in the document
it found it, and what it compared it against* - in seconds, not by re-reading the PDF.
Her fear is a silent wrong approval; her frustration is retyping the same amendment
email four times per shipment.

**Wei - SU dispatch coordinator.** His job feels done when the documents are emailed.
What ruins his week is the drip-feed: CG replies with one problem, he fixes it, a new
reply finds another. He wants **one complete list of everything wrong, the first
time**, so one correction cycle closes the shipment.

## JTBDs

1. *When a supplier's document set arrives in my inbox, I want every field already
   verified against the customer's rules - with the source text shown for anything
   flagged - so that I only spend my attention on exceptions, not on reading three
   PDFs per shipment.* (Priya)

2. *When something in my documents is wrong, I want a single reply listing every
   discrepancy as "document / field: found X, expected Y", so that I can fix
   everything in one resubmission instead of 2–4 cycles.* (Wei)

## North-star metric

**Median time from SU email received → CG-approved reply sent** (measured per
shipment from `received_at` to `sent_at`, both already recorded). Today this is
hours-to-days of manual reading plus queue time; the target is **< 15 minutes** for a
clean or clearly-discrepant set. A CG team lead can read this number off the stored
data on Day 14 and see whether validation actually got faster - and its guardrail
twin (**zero sent approvals later found wrong**) catches the failure mode of getting
faster by getting sloppier.

## The worst thing the agent could do - and how it's stopped

**Silently approve a wrong document set**, e.g. hallucinate a matching HS code, or
approve an invoice and packing list that each pass the rules but contradict each
other - sending bad paper toward customs with CG's name on it. Four independent
stops, all mechanical rather than vibes:

1. **Grounding:** every extracted value must carry a verbatim source quote; no quote
   ⇒ the field is `uncertain` ⇒ human review, never approval.
2. **Cross-document consistency:** consignee, HS code, weight, incoterm, ports and
   invoice number must agree across BOL/Invoice/Packing List; a contradiction forces
   an amendment even when every document passes individually (this exact case is in
   the demo and the test suite).
3. **Escalate on doubt:** any inconclusive check - low confidence, missing field,
   failed semantic verdict, provider outage - becomes `uncertain`, and one uncertain
   field routes the whole shipment to human review.
4. **The agent cannot send.** Its terminal state is `pending_review`; the only path
   to `sent` is Priya's button, and what she actually sent is stored beside what the
   agent drafted (audit trail).
