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
<the spec: facts with file:line, the rule, acceptance rows, "Not in this ticket">
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
- Holds the whole spec: facts with file:line, the rule, acceptance rows, a "Not in this ticket" section. Acceptance rows include one row on a case the ticket does not list. A self-improvement change (GLOSSARY) carries a `## Recount` block: the incident, how it is counted, the window, the count.
- A `core-issue` ticket states the line `Flow fix: yes | no (<why>)`. `yes` when the ticket is a Flow fix (GLOSSARY "Flow fix"); `yes` also adds the label `flow-fix` in the same create or edit call. The tracker guard refuses a core-issue create without the line.

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

Not in this ticket: the shipping rates themselves; the payment page's layout.
```
