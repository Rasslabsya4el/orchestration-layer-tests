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
- **Why:** <слово человека с датой или замеченный дефект>
- **Plan:** <одно-два предложения>
- **Result:** <число(а) и тест, который их показывает>
- **Undo:** two-way door (one revert) | one-way door: <что нельзя вернуть>; touches <кого или что затрагивает>

## For the agent
<the spec: facts with file:line, the rule, acceptance rows, "Not in this ticket">
```

The five fields keep this order and none is empty. The heading `## For the human`, the bold field names, the title and the whole `## For the agent` part stay English (the title rule and the guard read them).

## Rules

Title:
- English, plain product words, at most 80 characters.
- Names the change as the human sees it: no file name, no issue number.
- A ticket that waits on a named event carries the label `parked` and a `Parked: <event>` line at the top of the body, never the word in its title.

Human block:
- The text of the five fields is Russian. No file:line, no file path, no code identifier. The domain's own terms stay, written exactly as the domain's tool writes them.
- Result names a number and the test that shows it; when the spec has no number, write "not set yet".
- Undo ends with the blast radius: "touches <who or what>".
- A duration reads at a glance: seconds only under ~90 s, then minutes, then hours and minutes. Never a bare count of hundreds of seconds.

Agent block:
- Holds the whole spec: facts with file:line, the rule, acceptance rows, a "Not in this ticket" section. A self-improvement change (GLOSSARY) carries a `## Recount` block: the incident, how it is counted, the window, the count.
- A `core-issue` ticket states the line `Flow fix: yes | no (<why>)`. `yes` when the ticket is a Flow fix (GLOSSARY "Flow fix"); `yes` also adds the label `flow-fix` in the same create or edit call. The tracker guard refuses a core-issue create without the line.

## Where the shape comes from

The three ideas come from the mattpocock-skills `pr` skill: Summary stays small (What, Why, Plan); Evidence is a test and its number (Result); Merge Danger is one-way or two-way door plus blast radius (Undo).

## Example

```
Title: Checkout shows the shipping cost before payment

## For the human
- **What:** стоимость доставки показывается на странице корзины; сейчас она появляется только после ввода карты.
- **Why:** слово владельца магазина, <дата>: покупатели бросают заказ, когда итоговая цена их удивляет.
- **Plan:** корзина спрашивает тот же расчёт доставки, что и страница оплаты, как только введён почтовый индекс.
- **Result:** брошенных корзин на шаге оплаты меньше 30 %; показывают тест оформления заказа на трёх индексах и еженедельный отчёт по корзинам.
- **Undo:** two-way door (one revert); touches каждый заказ, сделанный через страницу корзины.

## For the agent
<facts with file:line, the rule, acceptance rows, "Not in this ticket">
```
