---
name: write-ticket
description: Use when opening a GitHub issue or rewriting an issue's title or body.
---

# Write a ticket

A ticket has two readers: the human decides from five plain lines written in Russian, the agent works from the spec below them, written in English.
The tracker guard refuses a ticket opened or rewritten without the human block, so write it first.

Read `docs/agents/local/write-ticket.md` when it exists: this repo's own filled example and terms.

## Template

```
Title: <the change as the human sees it, plain words>

## For the human
- **What:** <одно предложение: что меняется>
- **Goal:** <одно предложение: что должно стать правдой и для кого; для дефекта: что он ломает и для кого>
- **Plan:** <одно-два предложения>
- **Result:** <число(а) на всём классе случаев и проверка, которая их показывает; хотя бы один случай вне названных в тикете>
- **Undo:** two-way door (one revert) | one-way door: <что нельзя вернуть>; touches <кого или что затрагивает>

## For the agent
### How it works
Kind: <defect | missing capability | rule change | ...>
Class: <what every member shares, in abstract terms> (a change with no class: `Class: none (<why>)`, and no Members lines)
Members named: <the cases this ticket names>
Members not named: <at least one real or planted member the named ones do not cover>
Covers the class by: <the one mechanism that holds for every member; a defect: this case's mitigation and the check that fails for every member>
Logic: <how the system works after the change: what comes in, what decides, what comes out, what happens when a part fails>

<the spec: facts with file:line, the rule, acceptance rows with behaviour rows among them, "Not in this ticket">
Outside case: <one line: the member from Members not named that a behaviour row runs>
Same object: <every other part acting on the objects this change touches, and what happens when both run> (or, when nothing else acts on them: `none (<why>)`)
```

The five fields keep this order (What, Goal, Plan, Result, Undo) and none is empty. The heading `## For the human`, the bold field names, the title and the whole `## For the agent` part stay English (the title rule and the guard read them).
A body that still has `Why` is accepted on edit; create takes only `Goal`.

## Rules

Title:
- English, plain product words, at most 80 characters.
- Names the change as the human sees it: no file name, no issue number.
- A ticket that waits on a named event carries the label `parked` and a `Parked: <event>` line at the top of the body, never the word in its title.

Human block:
- The text of the five fields is Russian. No file:line, no file path, no code identifier. The domain's own terms stay, written exactly as the domain's tool writes them.
- Result names the number across the class and the check that shows it, with at least one case outside those the ticket names; when the spec has no number, write "not set yet".
- Undo ends with the blast radius: "touches <who or what>".
- A duration reads at a glance: seconds only under ~90 s, then minutes, then hours and minutes. Never a bare count of hundreds of seconds.

Agent block:
- Opens with `### How it works`: the classification first (Kind, Class, the members named and not, how the change covers the class), then the Logic, in the abstract terms the result is judged against. The writer classifies, not the agent: a defect is fixed as its class, never as the one case in view.
- Holds the whole spec: facts with file:line, the rule, acceptance rows, a "Not in this ticket" section.
- The acceptance rows check the Logic by behaviour: a behaviour row (its check starts with `behaviour:`) runs the working system the way it is used (a real chat, a real worker on a real task, a real merge) and reads what it did. A row that reads a text, or has a role restate the rule, checks a part, never the Logic; every ticket has behaviour rows.
- A row closes on runs that can be made now. In a ticket other tickets wait on, a check that waits on the calendar (nights, a week, "the next 10 merges") is a `Watch after close:` line under the table, not a row: it names what is watched, for how long, and what reports a bad result by itself (a check that opens its own ticket); a trend it would measure gets a row on one run now (a fixed count, a bound). The closing comment names the watch's first date.
- The writer makes the outside case, not the agent: a real member of the class the named cases do not cover, or a planted one (a throwaway test outside the list that the check must catch). It stands in `Members not named`, a behaviour row runs it, and the `Outside case:` line names it. A self-improvement change (GLOSSARY) carries a `## Recount` block: the incident, how it is counted, the window, the count.
- A `core-issue` ticket states the line `Flow fix: yes | no (<why>)`. `yes` when the ticket is a Flow fix (GLOSSARY "Flow fix"); `yes` also adds the label `flow-fix` in the same create or edit call. The tracker guard refuses a core-issue create without the line.
- The `Same object:` line is the writer's conclusion, written from a researcher's list of every other part (role, skill, hook, script, setting, open ticket) that acts on each object the change touches (a ticket, a label, a file, a merge, a test run), with file:line; it says what happens when both run, or `none (<why>)`. The tracker guard refuses a create without it.

## Where the shape comes from

The three ideas come from the mattpocock-skills `pr` skill: Summary stays small (What, Goal, Plan); Evidence is a test and its number (Result); Merge Danger is one-way or two-way door plus blast radius (Undo).

## Example

```
Title: Checkout shows the shipping cost before payment

## For the human
- **What:** стоимость доставки показывается на странице корзины; сейчас она появляется только после ввода карты.
- **Goal:** покупатель видит итоговую цену заказа до оплаты, а не в её конце.
- **Plan:** корзина спрашивает тот же расчёт доставки, что и страница оплаты, как только введён почтовый индекс.
- **Result:** стоимость доставки в корзине равна цене на оплате на всех четырёх индексах: трёх из тикета (10115, 20095, 80331) и одном, не названном в тикете (50667, другая зона); показывает тест расчёта доставки на этих четырёх. Патч, который зашивает три названных индекса, на не названном в тикете индексе 50667 провалит проверку. Брошенных корзин на шаге оплаты меньше 30 %, по еженедельному отчёту.
- **Undo:** two-way door (one revert); touches каждый заказ, сделанный через страницу корзины.

## For the agent
### How it works
Kind: defect.
Class: a price the buyer sees before payment that is computed apart from the price charged, so the two can differ.
Members named: the cart's shipping cost for 10115, 20095, 80331.
Members not named: 50667, a zone none of the three is in; any later price line the cart shows before payment.
Covers the class by: the cart shows what the payment page's own quote returns, one function for both pages, so no postcode or zone can differ; this case is mitigated by the same call.
Logic: the buyer types a postcode; the cart asks the payment page's quote with it and shows what comes back; a failed quote shows "not known yet", never a cached or guessed number.

Facts: the payment page prices shipping with `shipping_quote(postcode)` (checkout/payment.py:88); the cart page has no call to it (checkout/cart.py:31).
Named postcodes: 10115, 20095, 80331.
Rule: the cart calls the same `shipping_quote(postcode)` as the payment page, with the postcode the buyer typed; no second price table and no postcode literal in the cart code.
Acceptance:

| postcode | check | pass |
|---|---|---|
| 10115 | cart shipping cost for 10115 | shown cart cost = checkout page cost for 10115 |
| 20095 | cart shipping cost for 20095 | shown cart cost = checkout page cost for 20095 |
| 80331 | cart shipping cost for 80331 | shown cart cost = checkout page cost for 80331 |
| 50667 | cart shipping cost for 50667 (not named: a zone none of the three is in) | shown cart cost = checkout page cost for 50667 |
| 50667 live | behaviour: a buyer in the staging shop types 50667 in the cart, then pays | the cost the cart showed = the shipping line charged |

Outside case: 50667, a zone none of 10115, 20095, 80331 is in (the table's last two rows).
Same object: the payment page's `shipping_quote(postcode)` call (checkout/payment.py:88) reads the same postcode; with both, cart and payment show one price from one function, no second table.
Not in this ticket: the shipping rates themselves; the payment page's layout.
```
