# NOVA's personality — how it works

NOVA's character is a behavioural layer above whichever model is thinking. It
changes *how she decides to respond* — how sure to sound, when to disagree,
when to joke, how to own a mistake, how to talk about her agents' work — not
just the adjectives in a prompt. Live results against real models are in
[`NOVA_PERSONALITY_EVAL.md`](NOVA_PERSONALITY_EVAL.md).

## A. Where the personality lives

```text
                ┌──────────────────────── nova_personality.py ────────────────────────┐
                │ Layer A  identity: policies + example exchanges (same for everyone) │
                │ Layer B  adaptation: this account's explicit preferences            │
                │ clean_reply()  deterministic guard against filler openers           │
                │ task_outcome_message()  NOVA's voice for finished background work   │
                │ AGENT_TEMPERAMENTS  role temperaments for NOVA's agents              │
                └───────────────┬───────────────────────┬───────────────────┬─────────┘
                                │                       │                   │
   nova.py                      │                       │                   │
   NOVA_CORE (what she knows / may do: tools, truthfulness about actions, OMNIEL)
   NOVA_SYSTEM_PROMPT  = core + render("text")     ──▶ typed chat, gateway, any provider
   NOVA_VOICE_PROMPT   = core + render("voice")    ──▶ Gemini Live
   NOVA_OFFLINE_PROMPT = compact core + render("offline")  ──▶ Ollama (~1.5 KB, was ~12 KB)
                                │
   desk/bridge.py  typed chat:  + identity block (name, occupation, "about")
                                + adaptation_block()   (per request, per account)
                                learn_from_message(what they typed)
                                _persona_filter(): clean_reply on the streamed opening
   desk/live_session.py voice:  NOVA_VOICE_PROMPT + identity + adaptation_block()
                                learn_from_message(each finished spoken turn)
   task_manager.py              task_outcome_message() for "done / partly / failed"
   agent/planner.py             + render("agent:planner")
```

Before this, the personality was one "Directness" paragraph inside a 12 KB
prompt, the same prompt was sent unchanged to 1–3B offline models, typed chat
never received the identity block that voice did, and finished tasks were
announced as `Task 'X' completed.`

## B. How the behaviour fits together

| Policy | What it makes NOVA do |
|---|---|
| **Confidence follows evidence** | Four registers — plain statement / "I think, I'll confirm" / "I don't know yet" / "the evidence conflicts". Never bluffs to sound competent. |
| **Pushback** | Only when it matters (contradiction, wrong problem, risk, false premise, needless complexity, conflict with a stated goal). Shape: disagree → why → tie to their goal → better route. Levels: small correction, clear disagreement, strong "No", stop for real danger. Never at the person. |
| **User control** | After disagreeing once with reasons, she does what they decide (or says what she needs). Disagreement is advice, not refusal. |
| **Corrections** | Her mistakes: "You're right — I had X wrong; it's Y", redo, move on. Their corrections: checked, not accepted on assertion. |
| **Humour** | Dry, situational, occasional; always followed by substance; absent in serious or painful moments. |
| **Sound** | Answer first; length matches the problem; no filler openers or sign-offs; no catchphrases; never describes her own personality. |
| **Agents** | Her agents' work is her capability ("I'll check"); she weighs their output, sends weak work back, and gives her own conclusion. |
| **Truth first** | Any conflict between character and truth is resolved for truth. |

**Memory and adaptation (Layer B)** shape delivery, not identity. Explicit
requests only — "shorter answers", "no jokes", "challenge me more", "stop
arguing", "be more technical", "plain English" — nothing inferred from tone or
topic, so nothing sensitive is guessed. Each change is stored with the words
that caused it, in the account's own folder. Bounded ranges keep NOVA
recognisable: even at the lowest pushback setting she still flags real risks
and false premises.

## C. The configuration

`nova_personality.py` holds Layer A (sections `_CORE`, `_ANCHORS`,
`_TEXT_ONLY`, `_VOICE_ONLY`, `_OFFLINE`, `AGENT_TEMPERAMENTS`). Layer B is a
per-account file, `%APPDATA%\NOVA\accounts\<id>\personality_prefs.json`:

```jsonc
{
  "verbosity": -1,          // -2 shortest .. 2 most thorough
  "humour": 0,              //  0 none .. 2 more
  "challenge": 1,           //  0 risks only .. 2 challenge assumptions early
  "depth": "technical",     // "" | "technical" | "plain"
  "evidence": [
    {"at": "2026-09-29T08:12:03Z", "said": "no jokes please", "effect": ["asked for less humour"]}
  ]
}
```

The Settings "Response style" choice folds into `verbosity`.

## D. Before / after

Real replies from the same model with the old and new prompts are in
[`NOVA_PERSONALITY_EVAL.md`](NOVA_PERSONALITY_EVAL.md) — every scenario, both
versions, with the judge's verdict.

## E. Tests

* `tests/test_personality.py` — structure and deterministic behaviour: every
  mode carries the policies, voice vs text shaping, no borrowed branding,
  anchors don't share openings, adaptation learns only from explicit requests,
  is bounded, per account, and keeps its evidence; the filler guard removes
  only fixed openers (and not "Absolutely not — that deletes your backups");
  the streaming filter; task outcomes never soften failure; typed chat carries
  identity and adaptation.
* `tests/test_directness_prompt.py` — the pushback policy reaches all three
  model paths; the offline prompt stays small.
* `tests/eval/personality_eval.py` — live evaluation against Gemini (before vs
  after) and the local Ollama model. Run it after any prompt change:
  `python tests/eval/personality_eval.py --offline`.

## F. Drift protection

1. **Identity is never in the conversation history.** It is the system
   instruction on every request (text), for the whole session (voice), and is
   rebuilt each turn, so a long chat cannot push it out of context.
2. **Examples, not adjectives.** Six short exchanges show the behaviour; models
   drift back toward generic phrasing far less when they have a pattern to copy.
3. **A deterministic guard.** `clean_reply()` removes filler openers from typed
   replies whatever the model does — drift is removed, not merely discouraged.
4. **Regression tests.** The eval harness includes a scenario late in a long
   conversation; it and the structural tests fail if the personality weakens.
5. **Small footprint.** ~5 KB of policy online, ~1 KB offline, so it does not
   crowd out memory or tools.

## G. Model independence

The same Layer A text reaches every path: Gemini text, Gemini Live, anything
behind the NOVA gateway, a user's own key, and local models. Nothing depends on
a provider feature. Local models get the compressed version (the full one is
too long for a 1–3B model to follow). Offline limits are phrased in character:
"I can't do that offline", never "as an AI language model".

## H. Voice

Voice uses `render("voice")`: one or two sentences unless asked, no lists or
markdown, a one-word answer is fine, and she may cut in briefly when someone is
about to act on a wrong assumption. The eval's voice scenario checks length and
the absence of markdown. The reply filter does not touch audio (Gemini Live
speaks directly), so the spoken path relies on the prompt; a physical listening
test is still needed to judge rhythm and tone.

## I. Regression

The layer touches prompt assembly, the chat stream wrapper, the voice session's
setup and turn end, task announcements and the planner's instruction. Voice,
barge-in, memory, tools, agents, tasks, onboarding, permissions, updates and
account isolation keep their code paths; see the full suite result recorded
with the commit.
